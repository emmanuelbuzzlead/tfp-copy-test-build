"""research_inputs = "jina_news": Jina pages picked by the client's words (safety first), repeated lines dropped, length capped."""
from app.clients import jina

WORDS = ("location", "facilit", "plant", "safety", "ehs", "career", "product", "about", "logistic")

PAGES = {
    "https://x.com": "Welcome to X Steel\nNav: Home | About\nSee our [Products](https://x.com/products) and [Safety](https://x.com/safety).\n"
                     "[Plant locations](https://x.com/locations) [Blog post](https://x.com/blog/post) [Careers](https://x.com/careers)\n"
                     "Links/Buttons\n[Contact](https://x.com/contact)",
    "https://x.com/safety": "Nav: Home | About\nZero Harm every shift\nOur target zero program",
    "https://x.com/locations": "Nav: Home | About\nGary, IN plant\nDecatur, AL plant",
    "https://x.com/products": "Nav: Home | About\nHot rolled coil and plate",
    "https://x.com/careers": "Nav: Home | About\nWe are hiring",
}


def fake_fetch(url, max_chars=0, links_summary=False):
    return PAGES.get(url.rstrip("/"), "")


def test_spec_pages_are_ordered_safety_locations_products_and_deduped(monkeypatch):
    monkeypatch.setattr(jina, "_fetch_url", fake_fetch)
    text = jina.website_text_spec("x.com", WORDS, 5, 25000)
    order = [text.index(f"=== PAGE: https://x.com{p}") for p in ("", "/safety", "/locations", "/products")]
    assert order == sorted(order)                      # home, safety, locations, products
    assert "Zero Harm every shift" in text and "Hot rolled coil" in text and "We are hiring" in text   # careers allowed in spec mode
    assert "/blog/post" not in text.split("===")[-1] and "=== PAGE: https://x.com/blog" not in text
    assert text.count("Nav: Home | About") == 1        # repeated nav line kept once


def test_spec_pages_respect_the_character_cap(monkeypatch):
    monkeypatch.setattr(jina, "_fetch_url", lambda url, max_chars=0, links_summary=False: ("line %d\n" % hash(url)) * 3000 if url.endswith("x.com") else "")
    assert len(jina.website_text_spec("x.com", WORDS, 5, 5000)) <= 5000


def test_unreadable_home_returns_empty(monkeypatch):
    monkeypatch.setattr(jina, "_fetch_url", lambda *a, **k: "")
    assert jina.website_text_spec("dead.com", WORDS) == ""


def test_google_search_adds_snippets_and_fetches_unread_same_site_hits(monkeypatch):
    import dataclasses
    from app import client
    from app.clients import serper
    from app.stages import s5_copy
    prof = dataclasses.replace(client.profile(), research_search_queries=('site:{domain} safety "zero harm"',), research_search_fetch=1, research_max_chars=25000)
    hits = [{"title": "Safety at X", "link": "https://x.com/our-safety-program", "snippet": "Target Zero since 2019"},
            {"title": "X on LinkedIn", "link": "https://linkedin.com/company/x", "snippet": "steel"}]
    monkeypatch.setattr(serper, "web", lambda q, limit=5: hits)
    monkeypatch.setattr(jina, "_fetch_url", lambda url, max_chars=0, links_summary=False: "Our Target Zero program text")
    lines, extra = s5_copy._search_inputs(prof, "x.com", "X Steel", "home text without that url")
    assert "Target Zero since 2019" in lines and "linkedin.com/company/x" in lines
    assert "=== PAGE: https://x.com/our-safety-program (found by Google search) ===" in extra and "Target Zero program text" in extra
    assert "linkedin" not in extra                       # only same-site hits are fetched
    lines2, extra2 = s5_copy._search_inputs(prof, "x.com", "X Steel", "already has https://x.com/our-safety-program in it")
    assert extra2 == ""                                  # a page already read is not fetched twice
