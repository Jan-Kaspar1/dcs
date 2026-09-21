//! The monitoring page's robustness contract — the consolidated QA
//! findings on `dcs-monitor`'s served page: the page must recover when
//! a restarted source's history seqs begin again at 1 instead of
//! starving a stale since-cursor, bound every polling request with an
//! abort deadline and name the feed disconnected rather than freezing,
//! and parse Boolean command/force input strictly so arbitrary text is
//! refused rather than coerced to `false`. These are source-level
//! assertions over the single-page asset; `feed_state.rs` mirrors the
//! detection rules behaviorally against a live rig, including a real
//! monitor restart.

use dcs_monitor::PAGE;

/// A restarted source's served streams name a new per-boot
/// `generation`; the page compares it — or, for a peer that cannot
/// name one, the seq/tick domains' own regression — and answers the
/// reset rather than starving on seqs the new lifetime never serves.
#[test]
fn page_detects_the_sources_seq_domain_reset() {
    let page = PAGE;
    // The served stamps the detector reads: the publication section's
    // generation and each history answer's.
    for needle in ["health.generation", "history.generation"] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // The reset rule: a changed generation is the certain mark, and
    // the published-seq/tick regression is the fallback for a payload
    // that cannot name one.
    for needle in [
        "function publicationReset(last, current)",
        "last.generation !== current.generation",
        "current.published < last.published",
        "current.tick < last.tick",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // The recovery the mark triggers: every since-cursor returns to 0
    // so the next read refetches the retained head of the new seq
    // domain, the drawn trend series resets with its cursor, and the
    // feed line names the restart.
    for needle in [
        "function resetStreams()",
        "state.lastSeq = 0",
        "journalSince = 0",
        "state.samples = []",
        "feed.reset",
        "source restarted",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
}

/// Every request the page issues rides `fetchBounded`'s abort
/// deadline, and a poll that lands nothing marks the feed
/// disconnected instead of pinning the page on a hung connection.
#[test]
fn page_bounds_every_request_and_names_disconnects() {
    let page = PAGE;
    // The bounding machinery: one deadline, one controller per
    // request, the abort translating into a named timeout.
    for needle in [
        "const POLL_DEADLINE_MS",
        "new AbortController()",
        "controller.abort()",
        "signal: controller.signal",
        "function fetchBounded(resource, options)",
        "request timed out after",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // Every issued request is bounded: the only bare `await fetch(`
    // in the page is the one inside fetchBounded itself — polls,
    // commands, and the signal index all go through it.
    assert_eq!(
        page.matches("await fetch(").count(),
        1,
        "a request bypasses the bounded fetch"
    );
    assert!(
        page.matches("fetchBounded(").count() >= 10,
        "expected the polls and posts to ride fetchBounded"
    );
    // The failed poll's named state — and its clearing on the next
    // served answer.
    for needle in ["feed.offline", "feed disconnected", "\"offline\""] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
}

/// Boolean command and force input parses strictly: only the named
/// tokens reach the wire — arbitrary text is refused client-side
/// rather than coerced to a real `false` write.
#[test]
fn page_parses_boolean_input_strictly() {
    let page = PAGE;
    // The strict parser and its token set.
    for needle in [
        "function parseBool(text)",
        "\"true\"",
        "\"false\"",
        "return null",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // The coercion is gone: nothing maps non-`true` text to `false`,
    // and the old "anything unmatched is false" test is absent.
    for absent in ["/^(true|1|on)$/i", "{ bool: text === \"true\" }"] {
        assert!(!page.contains(absent), "page still coerces via {absent}");
    }
    // Every value submission guards on the strict parse: the point
    // row's write and force, the command form's write and force, the
    // interface row's typed arguments, and the parameter edit — each
    // refuses a null parse before a command is built.
    assert!(
        page.matches("value === null").count() >= 4,
        "a commandValue caller submits without guarding the parse"
    );
    for needle in [
        "not a \" + meta.value_type + \" value",
        "not a bool value",
        "not a Bool value",
        "parsed.value === null",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // Only a parsed token reaches the wire.
    assert!(
        page.contains("{ bool: parsed }"),
        "page lacks the parsed Bool value"
    );
}
