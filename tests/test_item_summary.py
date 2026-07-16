"""Item descriptions on /f/{id}: sanitization, excerpt, and Read-more
toggle behavior. Feed content is untrusted — the sanitizer is the
security boundary between arbitrary feed HTML and the page."""

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.utils import clean_summary, summary_excerpt, summary_was_truncated


def _client():
    import app.main
    return TestClient(app.main.app)


def _seed_feed(summary: str) -> int:
    import app.db as db_mod

    with Session(db_mod.engine) as s:
        feed = db_mod.Feed(
            url="https://blog.example.com/feed.xml",
            title="Blog",
            site_url="https://blog.example.com",
        )
        s.add(feed)
        s.commit()
        s.refresh(feed)
        s.add(db_mod.FeedItem(
            feed_id=feed.id, guid="post-1",
            title="First post",
            link="https://blog.example.com/1",
            summary=summary,
        ))
        s.commit()
        return feed.id


def test_clean_summary_strips_script():
    dirty = "<p>hi</p><script>alert(1)</script>"
    out = clean_summary(dirty)
    assert "<p>hi</p>" in out
    assert "<script>" not in out
    assert "alert" not in out


def test_clean_summary_preserves_safe_tags_and_href():
    src = '<p><strong>bold</strong> <a href="https://a.example">link</a></p>'
    out = clean_summary(src)
    assert "<strong>bold</strong>" in out
    assert 'href="https://a.example"' in out


def test_clean_summary_strips_headings():
    """Feeds sometimes embed the article's own <h1> into the summary
    (GitHub Trending, some Substack exports). Those blow up the visual
    rhythm of a feed listing where the item title IS the heading — so
    we strip h1–h6 and keep just their inner text."""
    src = "<h1>Big Heading</h1><p>body text</p>"
    out = clean_summary(src)
    assert "<h1" not in out
    assert "<h2" not in out
    assert "<h3" not in out
    # Inner text must survive.
    assert "Big Heading" in out
    assert "body text" in out


def test_clean_summary_strips_img_and_iframe():
    dirty = '<p>x</p><img src="x" onerror="alert(1)"><iframe src="/"></iframe>'
    out = clean_summary(dirty)
    assert "<img" not in out
    assert "<iframe" not in out
    assert "onerror" not in out


def test_clean_summary_forces_noopener_on_links():
    out = clean_summary('<a href="https://a.example">x</a>')
    assert "rel=" in out
    assert "noopener" in out


def test_excerpt_strips_html_and_truncates():
    long = "<p>" + ("word " * 200) + "</p>"
    ex = summary_excerpt(long, max_chars=50)
    assert "<" not in ex and ">" not in ex
    assert ex.endswith("…")
    assert len(ex) <= 55


def test_excerpt_short_input_returns_as_is():
    ex = summary_excerpt("<p>hello world</p>", max_chars=50)
    assert ex == "hello world"
    assert not ex.endswith("…")


def test_summary_was_truncated_flag():
    assert summary_was_truncated("<p>" + ("a " * 400) + "</p>") is True
    assert summary_was_truncated("<p>short</p>") is False
    assert summary_was_truncated(None) is False
    assert summary_was_truncated("") is False


def test_feed_page_renders_details_toggle_when_summary_is_long():
    long_html = "<p>" + ("Lorem ipsum dolor sit amet " * 30) + "</p>"
    feed_id = _seed_feed(long_html)

    r = _client().get(f"/f/{feed_id}")
    assert r.status_code == 200
    # Native <details> markup — no JS needed on the client.
    assert '<details class="item-summary">' in r.text
    assert ">Read more<" in r.text
    # Excerpt is plain text (no <p>).
    assert "Lorem ipsum" in r.text


def test_feed_page_renders_full_summary_inline_when_short():
    short = "<p>Just a quick note.</p>"
    feed_id = _seed_feed(short)

    r = _client().get(f"/f/{feed_id}")
    assert r.status_code == 200
    # No <details> when nothing was truncated — full inline.
    assert '<details class="item-summary">' not in r.text
    assert "Just a quick note" in r.text


def test_feed_page_omits_summary_block_when_empty():
    feed_id = _seed_feed("")
    r = _client().get(f"/f/{feed_id}")
    assert r.status_code == 200
    assert "item-summary" not in r.text


def test_feed_page_strips_script_end_to_end():
    feed_id = _seed_feed('<p>ok</p><script>window.pwn=1</script>')
    r = _client().get(f"/f/{feed_id}")
    assert r.status_code == 200
    # The base template legitimately embeds an inline script for the
    # favicon fallback — checking for "<script" globally would false-
    # positive. What we actually need: the injected payload didn't
    # survive sanitization.
    assert "window.pwn" not in r.text
