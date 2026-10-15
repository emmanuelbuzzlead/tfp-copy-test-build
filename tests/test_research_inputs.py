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
