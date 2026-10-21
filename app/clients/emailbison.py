"""EmailBison -- campaign listing and lead push. NOT used for performance metrics.

Verified against the live IDPR workspace on 2026-09-07:
  base            https://send.buzzlead.io
  workspace       id=10, name "Interdependence" (the only workspace this key sees)
  auth            Authorization: Bearer <key>, plus X-Workspace-Id
  pagination      per_page is IGNORED. Asking for 100 returned 15, last_page=6,
                  total=82. Every listing here therefore pages until last_page,
                  and any code that reads only page 1 is silently wrong.

Lead loading is a TWO-STEP flow, confirmed live on a throwaway campaign 2026-09-07
(spec gate 3, now closed). /leads/bulk and /leads/import do not exist, and POST on
/api/campaigns/{id}/leads returns 405 "Supported methods: GET, HEAD, DELETE":

  1. POST /api/leads/create-or-update/multiple   -> returns lead ids (batch 100)
  2. POST /api/campaigns/{id}/leads/attach-leads -> assigns them (batch 500)

Custom variables must be REGISTERED on the workspace first or the upsert 422s the whole
batch, and their names are lowercased by the API ("PERSONALIZATION 1" -> "personalization 1").

Removing a lead from a campaign is DELETE /api/campaigns/{id}/leads with {"lead_ids": []}.
That is what company-level stop-on-reply uses. It genuinely removes the lead from the
campaign, which also decrements EmailBison's own total_leads -- see ve_campaigns
.pushed_lead_count in db.py for why Stage 1 must not score against that number.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from typing import Any, Iterable

from ..config import settings
from .http import VendorError, request


class BulkLoadUnavailable(RuntimeError):
    """The lead-load endpoints rejected the payload; caller should fall back to CSV."""


UPSERT_BATCH = 100   # leads per /api/leads/create-or-update/multiple call
ATTACH_BATCH = 500   # lead ids per /api/campaigns/{id}/leads/attach-leads call


def _headers() -> dict[str, str]:
    if not settings.emailbison_api_key:
        raise RuntimeError("this client's EmailBison key is not set (see emailbison_key_env in its client.toml)")
    return {
        "Authorization": f"Bearer {settings.emailbison_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Workspace-Id": str(settings.workspace_id),
    }


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    return request("emailbison", "GET", f"{settings.emailbison_base}{path}",
                   headers=_headers(), params=params)


def _send(method: str, path: str, body: Any = None) -> Any:
    return request("emailbison", method, f"{settings.emailbison_base}{path}",
                   headers=_headers(), json_body=body)


# Three batches pushed in the same minute on 2026-09-14: one loaded, two fell to the CSV
# fallback on the first refused call. A throttled or 5xx response from a bulk route is
# a reason to wait, not to give up on 4,000 paid-for leads.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_RETRY_BACKOFF = (5, 10, 20, 40, 60)


def _send_retry(method: str, path: str, body: Any = None) -> Any:
    import time as _time
    last: Exception | None = None
    for attempt in range(len(_RETRY_BACKOFF) + 1):
        try:
            return _send(method, path, body)
        except VendorError as exc:
            transient = exc.status in _RETRY_STATUSES or exc.status is None
            if not transient or attempt == len(_RETRY_BACKOFF):
                raise
            last = exc
            print(f"[emailbison] {method} {path} -> {exc.status}; retry in "
                  f"{_RETRY_BACKOFF[attempt]}s", flush=True)
            _time.sleep(_RETRY_BACKOFF[attempt])
    raise last  # pragma: no cover


def _paged(path: str, params: dict[str, Any] | None = None, cap: int = 400) -> list[dict]:
    """Page to last_page. per_page is ignored server-side (15 enforced), so this must."""
    out: list[dict] = []
    page, last = 1, 1
    while page <= last and page <= cap:
        payload = _get(path, {**(params or {}), "page": page})
        out.extend((payload or {}).get("data") or [])
        last = int(((payload or {}).get("meta") or {}).get("last_page") or 1)
        page += 1
    return out


def list_workspaces() -> list[dict]:
    for path in ("/api/workspaces/v1.1", "/api/workspaces"):
        try:
            payload = _get(path)
            return (payload or {}).get("data") or []
        except VendorError:
            continue
    return []


def list_campaigns() -> list[dict]:
    """Every campaign in the workspace, paging to last_page.

    per_page is ignored server-side (15 enforced), so this must page. Reading only
    the first response would report 15 of 82 campaigns with no error.
    """
    out: list[dict] = []
    page, last = 1, 1
    while page <= last and page <= 200:
        payload = _get("/api/campaigns", {"page": page, "per_page": 100})
        out.extend((payload or {}).get("data") or [])
        meta = (payload or {}).get("meta") or {}
        last = int(meta.get("last_page") or 1)
        page += 1
    return out


def campaign_index() -> dict[str, dict]:
    """{normalized campaign name: campaign} for matching CSV export rows to campaigns."""
    return {(c.get("name") or "").strip().lower(): c for c in list_campaigns()}


LEAD_FIELDS = ("email", "first_name", "last_name", "company_name", "website",
               "title", "personalization_1", "personalization_2")


def _lead_payload(contact: dict) -> dict:
    """Map a pipeline contact to EmailBison's lead shape.

    PERSONALIZATION 1/2 are the merge variables the Clay copy maps into (spec Stage 5
    step 9), carried as custom fields so variant-level reporting works.
    """
    custom_company: dict = {}
    try:
        from .. import client as _client
        if _client.profile().company_custom_field:
            from ..company_name_cleaner import clean_company_name
            custom_company = {"COMPANY": clean_company_name(contact.get("company_name") or contact.get("company_domain") or "")}
    except Exception:
        custom_company = {}
    if contact.get("materials_hauled"):
        custom_company["MATERIALS_HAULED"] = contact["materials_hauled"]     # the OG-copy campaigns merge {{MATERIALS_HAULED}} (app/materials.py)
    return {
        "email": contact["email"],
        "first_name": contact.get("first_name") or "",
        "last_name": contact.get("last_name") or "",
        "company_name": contact.get("company_name") or "",
        "website": contact.get("company_domain") or "",
        "custom_fields": {
            **custom_company,
            "title": contact.get("title") or "",
            "PERSONALIZATION 1": contact.get("personalization_1") or "",
            "PERSONALIZATION 2": contact.get("personalization_2") or "",
            "vertical": contact.get("vertical") or "",
            "research_brief": (contact.get("research_report") or "")[:2000],
            "experiment_id": contact.get("experiment_id") or "",
            "copy_variant": contact.get("copy_variant") or "",
            "title_segment": contact.get("title_segment") or "",
            "prompt_version": contact.get("prompt_version") or "",
        },
    }


def _lead_for_upsert(contact: dict) -> dict:
    """Pipeline contact -> the upsert API shape.

    custom_fields (our key) becomes custom_variables (API key) and names are lowercased,
    because that is how the API stores and matches them.
    """
    payload = _lead_payload(contact)
    custom = payload.pop("custom_fields", {})
    return {
        "email": payload["email"],
        "first_name": payload.get("first_name", ""),
        "last_name": payload.get("last_name", ""),
        "company": payload.get("company_name", ""),
        "title": contact.get("title") or "",
        "custom_variables": [
            {"name": k.lower(), "value": str(v)} for k, v in custom.items() if v
        ],
    }


def custom_variable_names() -> set[str]:
    try:
        payload = _get("/api/custom-variables")
    except VendorError:
        return set()
    items = (payload or {}).get("data") if isinstance(payload, dict) else payload
    return {
        (i.get("name") or "").strip().lower()
        for i in (items or []) if isinstance(i, dict) and i.get("name")
    }


def ensure_custom_variables(names: Iterable[str]) -> None:
    """Register any missing custom variables.

    The upsert 422s the WHOLE batch if it references a variable the workspace has not
    registered, so this runs before every push rather than only on first use.
    """
    wanted = {(n or "").strip().lower() for n in names if (n or "").strip()}
    if not wanted:
        return
    existing = custom_variable_names()
    for name in sorted(wanted - existing):
        try:
            _send("POST", "/api/custom-variables", {"name": name})
        except VendorError as exc:
            # "already been taken" is the desired end state, not a failure: our GET
            # listing can be paginated or empty even when the variable exists.
            if "already been taken" not in str(exc).lower():
                print(f"[warn] could not create custom variable {name!r}: {exc}", flush=True)


def create_campaign(name: str) -> dict:
    payload = _send("POST", "/api/campaigns", {"name": name})
    return (payload or {}).get("data") or payload


def campaign_name_for(vertical: str, when: date | None = None) -> str:
    """VE-{vertical}-{YYYY-MM-DD}.

    A fresh campaign per vertical per batch is deliberate: per-vertical stats have to
    stay readable so the 90-day loop can score them, and the 1,000-lead sample gate is
    applied per campaign. Pooling batches into one standing campaign would blur every
    vertical together and make the winner selection meaningless.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "-", vertical or "vertical").strip("-") or "vertical"
    return f"VE-{slug}-{(when or date.today()):%Y-%m-%d}"


def push_leads(campaign_id: int, contacts: Iterable[dict], dry_run: bool = True,
               on_upserted=None, on_attached=None) -> dict:
    """Load contacts into a campaign via the confirmed two-step flow.

    on_upserted({email: lead_id}) fires after every upsert chunk and on_attached([emails])
    after every attach chunk, so the caller can persist progress as it happens. A push
    cut off half way (a redeploy of the web service, 2026-09-15 review) then resumes
    from the last chunk instead of starting over or leaving a half-loaded campaign
    that our records call approved.
    """
    contacts = list(contacts)
    leads = [_lead_for_upsert(c) for c in contacts]
    if dry_run:
        return {
            "dry_run": True,
            "campaign_id": campaign_id,
            "workspace": f"{settings.workspace_name} (id={settings.workspace_id})",
            "base": settings.emailbison_base,
            "lead_count": len(leads),
            "sample": leads[:3],
        }
    if not leads:
        return {"dry_run": False, "lead_count": 0, "attached": 0}

    ensure_custom_variables(
        {n["name"] for lead in leads for n in lead.get("custom_variables", [])}
    )

    lead_ids: list[int] = []
    _last_id_map: dict[str, int] = {}
    try:
        for i in range(0, len(leads), UPSERT_BATCH):
            chunk = leads[i:i + UPSERT_BATCH]
            payload = _send_retry("POST", "/api/leads/create-or-update/multiple",
                                  {"leads": chunk, "existing_lead_behavior": "patch"})
            data = (payload or {}).get("data")
            if not isinstance(data, list):
                raise BulkLoadUnavailable(f"unexpected upsert response: {str(payload)[:200]}")
            # Key by EMAIL, never by position. The response is not in request order
            # (2026-09-15: a contact's stored id was three off, so a later removal by id
            # would have pulled the wrong person out of the campaign). Every item
            # carries its email, so map on that and fall back to position only when
            # an item has none.
            by_email = {(x.get("email") or "").lower(): x.get("id")
                        for x in data if isinstance(x, dict) and x.get("id")}
            chunk_contacts = contacts[i:i + UPSERT_BATCH]
            got = []
            for pos, c in enumerate(chunk_contacts):
                lid = by_email.get((c.get("email") or "").lower())
                if lid is None and pos < len(data) and isinstance(data[pos], dict)                         and not data[pos].get("email"):
                    lid = data[pos].get("id")
                if lid is not None:
                    got.append(lid)
            lead_ids.extend(got)
            for c in chunk_contacts:
                e = (c.get("email") or "").lower()
                if e in by_email:
                    _last_id_map[e] = by_email[e]
            if on_upserted:
                try:
                    on_upserted({(c.get("email") or "").lower(): by_email[(c.get("email") or "").lower()]
                                 for c in chunk_contacts
                                 if (c.get("email") or "").lower() in by_email})
                except Exception as exc:  # progress is best effort, never fatal
                    print(f"[emailbison] progress not recorded: {exc}", flush=True)

        # EmailBison refuses to attach a lead that is already "in sequence" in ANOTHER campaign, and in a mixed batch it skips
        # those silently (found 2026-10-09: 9 of 45 leads were marked pushed but were never in the campaign). Check each lead's
        # other campaigns first, attach only the ones that can be attached, and tell the caller who was refused.
        email_by_id = {v: k for k, v in _last_id_map.items()}
        refused: dict[str, list[int]] = {}

        def _busy(lid: int) -> tuple[int, list[int]]:
            try:
                d = (_get(f"/api/leads/{lid}") or {}).get("data") or {}
            except Exception:
                return lid, []
            return lid, [int(x["campaign_id"]) for x in (d.get("lead_campaign_data") or [])
                         if str(x.get("status")).lower() == "in_sequence" and x.get("campaign_id") is not None
                         and int(x["campaign_id"]) != int(campaign_id)]

        from concurrent.futures import ThreadPoolExecutor as _Pool
        with _Pool(max_workers=8) as _pool:
            for lid, camps in _pool.map(_busy, lead_ids):
                if camps:
                    refused[email_by_id.get(lid, str(lid))] = camps
        eligible_ids = [lid for lid in lead_ids if email_by_id.get(lid, str(lid)) not in refused]

        attached = 0
        for i in range(0, len(eligible_ids), ATTACH_BATCH):
            chunk = eligible_ids[i:i + ATTACH_BATCH]
            try:
                _send_retry("POST", f"/api/campaigns/{campaign_id}/leads/attach-leads", {"lead_ids": chunk})
                attached += len(chunk)
            except VendorError as exc:
                if exc.status == 422 and "No leads were added" in str(exc):
                    for lid in chunk:      # every lead in the chunk was refused (bounced, unsubscribed, or in a sequence)
                        refused.setdefault(email_by_id.get(lid, str(lid)), [])
                    continue
                raise
            if on_attached:
                try:
                    on_attached([email_by_id[lid] for lid in chunk if lid in email_by_id])
                except Exception as exc:
                    print(f"[emailbison] progress not recorded: {exc}", flush=True)
    except VendorError as exc:
        raise BulkLoadUnavailable(
            f"lead load failed ({exc.status}); fall back to CSV handoff. {exc}"
        ) from exc

    # Pair each contact with its EmailBison lead id so stop-on-reply can remove siblings.
    # Rebuilt from the per-chunk email map, not by zipping positions.
    id_by_email = dict(_last_id_map)

    return {"dry_run": False, "campaign_id": campaign_id, "lead_count": len(leads),
            "attached": attached, "lead_ids": lead_ids, "id_by_email": id_by_email, "refused": refused}


def remove_leads(campaign_id: int, lead_ids: list[int]) -> int:
    """Remove leads from a campaign -- the stop-on-reply mechanism.

    Confirmed live: DELETE /api/campaigns/{id}/leads with {"lead_ids": [...]}. There is
    no stop/pause/detach route; those all 404. This removes the lead from the campaign
    entirely, which is why pushed_lead_count is recorded separately for scoring.
    """
    if not lead_ids:
        return 0
    removed = 0
    for i in range(0, len(lead_ids), ATTACH_BATCH):
        chunk = lead_ids[i:i + ATTACH_BATCH]
        _send("DELETE", f"/api/campaigns/{campaign_id}/leads", {"lead_ids": chunk})
        removed += len(chunk)
    return removed


def list_replies(campaign_id: int) -> list[dict]:
    """Every reply on a campaign. Carries lead_id, interested and automated_reply."""
    return _paged(f"/api/campaigns/{campaign_id}/replies")


def leads_csv(contacts: Iterable[dict]) -> str:
    """Upload-ready CSV for the manual-load fallback (spec Stage 7)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["email", "first_name", "last_name", "company_name", "website",
                     "title", "vertical", "PERSONALIZATION 1", "PERSONALIZATION 2"])
    for c in contacts:
        writer.writerow([
            c.get("email", ""), c.get("first_name", ""), c.get("last_name", ""),
            c.get("company_name", ""), c.get("company_domain", ""), c.get("title", ""),
            c.get("vertical", ""), c.get("personalization_1", ""), c.get("personalization_2", ""),
        ])
    return buf.getvalue()


def get_campaign(campaign_id: int) -> dict:
    """One campaign's live counters (total_leads, emails_sent, replied, interested)."""
    payload = _get(f"/api/campaigns/{campaign_id}")
    return (payload or {}).get("data") or payload or {}


def resolve_model_campaign(exclude: int | None = None) -> int:
    """A live VE campaign to duplicate the next campaign from.

    Troy, 2026-09-22: "ideally we can copy any running ve campaign as template." They
    are interchangeable -- verified that day, all nine live VE campaigns and the old
    model 1036 share ONE sequence fingerprint (3 steps, waits 3/3/1, plain_text true,
    2,400 max/day).

    Making it dynamic removes a whole class of failure rather than guarding against it.
    With a fixed model, ONE campaign was special: it could not be archived, its accounts
    could never be freed, and a `drop` verdict on it silently no-opped forever behind a
    warn line -- a live risk once the model became 1084, the current SCALE winner.
    Now the template is whichever campaign happens to be running, so archiving any one
    of them just moves it.

    Oldest first, so the template is the campaign least likely to be archived next. Falls
    back to settings.model_campaign_id when nothing live can be read.
    """
    try:
        rows = list_campaigns()
    except Exception:
        return settings.model_campaign_id
    live = [c for c in rows
            if str(c.get("name") or "").startswith("VE-")
            and str(c.get("status") or "").lower() in ("active", "paused")
            and int(c.get("id") or 0) != (exclude or 0)]
    if not live:
        return settings.model_campaign_id
    live.sort(key=lambda c: (c.get("created_at") or "", int(c.get("id") or 0)))
    return int(live[0]["id"])


def archive_campaign(campaign_id: int) -> dict:
    """Take a campaign out of rotation AND give its sender accounts back.

    Pausing is not enough and never was. senders.ACTIVE_STATUSES counts "paused"
    alongside "active", because a paused campaign still holds its accounts' slots and
    still owns their daily limit. Two of the campaigns blocking capacity on 2026-09-22
    had been paused for days and were still holding 12 accounts between them.

    `PATCH /api/campaigns/{id}/archive` is the route that actually releases them. It is
    undocumented but real: GET returns 405 and OPTIONS answers `allow: PATCH`. The pause
    routes stay on as a fallback so a campaign at least stops sending if archive moves,
    but the caller is told which one answered -- a fallback here means the accounts were
    NOT freed.
    """
    if campaign_id == resolve_model_campaign(exclude=campaign_id):
        # Only bites when this is the LAST campaign that could serve as a template.
        # Troy, 2026-09-22: "ideally we can copy any running ve campaign as template."
        # resolve_model_campaign picks any live VE campaign, so normally archiving one
        # just moves the template to another and this never fires.
        raise RuntimeError(
            f"campaign {campaign_id} is the only campaign left to duplicate new "
            f"campaigns from; archiving it would break every future push")
    last = None
    for method, path, body in (
        ("PATCH", f"/api/campaigns/{campaign_id}/archive", None),
        ("POST", f"/api/campaigns/{campaign_id}/pause", None),
        ("POST", f"/api/campaigns/{campaign_id}/status", {"status": "paused"}),
        ("PATCH", f"/api/campaigns/{campaign_id}", {"status": "paused"}),
    ):
        try:
            resp = request("emailbison", method, f"{settings.emailbison_base}{path}",
                           headers=_headers(), json_body=body) or {}
            # WHICH route answered is the whole point. Only /archive frees the sender
            # accounts; senders.ACTIVE_STATUSES counts "paused", so a pause leaves them
            # held. apply_drop used to log "archived" either way, and stage 8 orders
            # drops before scale-ups precisely because it believes the capacity came
            # back. Say so plainly instead.
            freed = path.endswith("/archive")
            return {**resp, "route": path, "method": method, "senders_freed": freed}
        except VendorError as exc:
            last = exc
    raise RuntimeError(f"no working pause/archive route for campaign {campaign_id}: {last}")


# ---------------------------------------------------------------------------
# campaign provisioning -- duplicate the model campaign rather than build one
# ---------------------------------------------------------------------------

def duplicate_campaign(model_id: int, name: str) -> dict:
    """Copy the model campaign and rename the copy.

    Verified against campaign 1036 on 2026-09-11: POST /api/campaigns/{id}/duplicate
    returns a new DRAFT that inherits the sequence, plain_text=True, open_tracking=False
    and the send limits. It does NOT inherit sender emails -- the copy starts with zero,
    which is why accounts have to be attached separately.

    Duplicating rather than creating is the point: a fresh campaign would carry none of
    the sequence or sending settings the model has been tuned with.
    """
    created = request("emailbison", "POST",
                      f"{settings.emailbison_base}/api/campaigns/{model_id}/duplicate",
                      headers=_headers())
    data = (created or {}).get("data") or created or {}
    campaign_id = data.get("id")
    if not campaign_id:
        raise RuntimeError(f"duplicate of {model_id} returned no id: {str(created)[:200]}")
    try:
        rename_campaign(campaign_id, name)
        data["name"] = name
    except VendorError as exc:
        # A copy under the wrong name is still usable; losing the campaign is not.
        print(f"[emailbison] could not rename {campaign_id}: {exc}", flush=True)
    return data


def start_campaign(campaign_id: int) -> dict:
    """Take a campaign out of draft so it sends in its schedule window.

    PATCH /api/campaigns/{id}/resume -- found 2026-09-15 by probing a throwaway copy
    of the model (/start, /launch, /activate are 404; /resume is PATCH-only). pause is
    the same shape at /pause.
    """
    r = request("emailbison", "PATCH",
                f"{settings.emailbison_base}/api/campaigns/{campaign_id}/resume",
                headers=_headers(), json_body={})
    return (r or {}).get("data") or r or {}


def pause_campaign(campaign_id: int) -> dict:
    r = request("emailbison", "PATCH",
                f"{settings.emailbison_base}/api/campaigns/{campaign_id}/pause",
                headers=_headers(), json_body={})
    return (r or {}).get("data") or r or {}


def sequence_step_waits(campaign_id: int) -> list[int]:
    """Days each sequence step waits, in order.

    There is no API to SET these: PATCH and PUT on /sequence-steps/{id},
    /campaigns/{id}/sequence-steps/{id} and their /update variants all 404, probed on a
    throwaway duplicate 2026-09-18. So the cadence can be verified but not corrected,
    and a wrong one has to be fixed on the template in the UI.
    """
    try:
        rows = (_get(f"/api/campaigns/{campaign_id}/sequence-steps") or {}).get("data") or []
    except Exception:
        return []
    rows = sorted(rows, key=lambda r: r.get("order") or 0)
    return [int(r.get("wait_in_days") or 0) for r in rows]


def rename_campaign(campaign_id: int, name: str) -> dict:
    """PATCH /api/campaigns/{id}/update. Verified 2026-09-14: the bare /campaigns/{id}
    route is GET/DELETE only, so the first three VE campaigns kept the model's name
    ("Copy of 2026 Funded companies 7th Sept") until renamed by hand."""
    r = request("emailbison", "PATCH",
                f"{settings.emailbison_base}/api/campaigns/{campaign_id}/update",
                headers=_headers(), json_body={"name": name})
    return (r or {}).get("data") or r or {}


def update_campaign_limits(campaign_id: int, max_new_leads_per_day: int,
                           max_emails_per_day: int) -> dict:
    """PATCH /api/campaigns/{id}/update. Verified 2026-10-05 (the Healthcare cap edit):
    the API validates max_new_leads_per_day <= max_emails_per_day against whatever is
    CURRENTLY stored for the other field, so sending only the one that is changing can
    422 even when the new value is well under the existing other one -- both fields
    must be sent together in the same request every time, regardless of which changed.
    """
    r = request("emailbison", "PATCH",
                f"{settings.emailbison_base}/api/campaigns/{campaign_id}/update",
                headers=_headers(),
                json_body={"max_new_leads_per_day": max_new_leads_per_day,
                          "max_emails_per_day": max_emails_per_day})
    return (r or {}).get("data") or r or {}


def get_schedule(campaign_id: int) -> dict:
    payload = _get(f"/api/campaigns/{campaign_id}/schedule")
    return (payload or {}).get("data") or {}


def set_schedule(campaign_id: int, start: str | None = None, end: str | None = None,
                 timezone: str | None = None) -> dict:
    """Force the sending window. The model campaign's own hours are 09:32-17:48, so a
    duplicate inherits those rather than the intended 9-7, and nothing would say so."""
    body = {
        "monday": True, "tuesday": True, "wednesday": True, "thursday": True,
        "friday": True, "saturday": False, "sunday": False,
        # The API validates "H:i" (verified 2026-09-14: "09:00:00" is rejected with 422).
        "start_time": (start or settings.send_start_time)[:5],
        "end_time": (end or settings.send_end_time)[:5],
        "timezone": timezone or settings.send_timezone,
        # Required by the API (422 without it, verified 2026-09-14); we never want the
        # window saved as a reusable template.
        "save_as_template": False,
    }
    last = None
    for method in ("PATCH", "PUT", "POST"):
        try:
            r = request("emailbison", method,
                        f"{settings.emailbison_base}/api/campaigns/{campaign_id}/schedule",
                        headers=_headers(), json_body=body)
            return (r or {}).get("data") or r or {}
        except VendorError as exc:
            last = exc
    raise RuntimeError(f"no working schedule route for campaign {campaign_id}: {last}")


def list_sender_emails(limit: int = 2000) -> list[dict]:
    """Every sender account in the workspace. per_page is ignored, so this pages.

    The old runaway guard was `page <= 200`. EmailBison enforces FIFTEEN rows a page
    whatever per_page asks for, so 200 pages is 3,000 accounts -- and this workspace has
    4,532 across 303 pages. Everything past page 200 was invisible.

    Measured 2026-09-23, and it was not a harmless tail: pages 201, 240 and 260 were
    100% SENDABLE accounts. senders.free_capacity() reported **0 free accounts, 0/day**
    while a full read of the same roster showed 360 accounts and 1,798/day. Stage 7
    holds a batch below 600/day, so the pipeline was refusing to launch campaigns onto
    capacity it owned and could not see, and align() was picking from two thirds of the
    roster.

    The guard now follows last_page instead of a constant, with a ceiling well above any
    real workspace so a broken meta block still cannot spin forever.
    """
    out, page, last = [], 1, 1
    while page <= last and len(out) < limit and page <= 2000:
        payload = _get("/api/sender-emails", {"page": page, "per_page": 100})
        out.extend((payload or {}).get("data") or [])
        last = int(((payload or {}).get("meta") or {}).get("last_page") or 1)
        page += 1
    return out[:limit]


def campaign_sender_emails(campaign_id: int) -> list[dict]:
    """A campaign's sender accounts. Paged: per_page is ignored (15 enforced), and the
    model campaign alone has more than one page, so a single read under-counted."""
    return _paged(f"/api/campaigns/{campaign_id}/sender-emails", cap=100)


def attach_sender_emails(campaign_id: int, sender_ids: list[int]) -> dict:
    """POST /api/campaigns/{id}/attach-sender-emails, verified 2026-09-14.

    Refuses ids already on the campaign with success:false and a message, which is
    not an error for our purposes: the account is attached either way.
    """
    if not sender_ids:
        return {"success": True, "attached": 0}
    payload = _send("POST", f"/api/campaigns/{campaign_id}/attach-sender-emails",
                    {"sender_email_ids": [int(i) for i in sender_ids]})
    data = (payload or {}).get("data") or payload or {}
    if data.get("success") is False and "already exist" not in str(data.get("message", "")):
        raise RuntimeError(f"attach refused: {data.get('message')}")
    return data
