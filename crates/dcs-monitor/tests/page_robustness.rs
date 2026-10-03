//! The monitoring page's robustness contract — the consolidated QA
//! findings on `dcs-monitor`'s served page: the page must recover when
//! a restarted source's history seqs begin again at 1 instead of
//! starving a stale since-cursor, bound every request it issues with
//! an abort deadline so a failed or hung read names the feed's
//! degraded state rather than pinning last-known values, and parse
//! Boolean command/force input strictly so arbitrary text is refused
//! rather than coerced to `false`. These are source-level assertions
//! over the single-page asset; `feed_state.rs` mirrors the detection
//! rules behaviorally against a live rig, including a real monitor
//! restart.

use dcs_monitor::PAGE;

/// A restarted source names its new process lifetime in-band: every
/// `/history` envelope carries the serving run's `run` ordinal, the
/// journal stream's `run_boundary` markers do the same for the durable
/// record, and the publication identity itself regresses — published
/// seq and tick both restart with the store. The page funnels all
/// three observations into one restart note that resets every
/// since-cursor rather than starving on seqs the new lifetime never
/// serves.
#[test]
fn page_detects_the_sources_seq_domain_reset() {
    let page = PAGE;
    // The served marks the detector reads: the history envelope's run
    // ordinal, the journal's run_boundary, the publication identity's
    // regression.
    for needle in [
        "history.run",
        "run_boundary",
        "current.published < last.published",
        "current.tick < last.tick",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // The one restart path every observation takes: the cursors and
    // the caught-up flags reset so the streams re-read whole, and the
    // feed line names the seam.
    for needle in [
        "function noteRestart(detail)",
        "state.lastSeq = 0",
        "state.run = null",
        "state.caughtUp = false",
        "journalSince = 0",
        "feed.restart",
        "source restarted",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
}

/// Every request the page issues rides `pollFetch`'s abort deadline —
/// polls, posts, and the one-time signal-index read alike — so a hung
/// connection aborts inside one poll period, and a poll that lands
/// nothing re-marks the feed stale rather than freezing on the last
/// fresh publication's bookkeeping.
#[test]
fn page_bounds_every_request_and_names_disconnects() {
    let page = PAGE;
    // The bounding machinery: one deadline constant, the shared
    // wrapper, the engine's abort signal on every request.
    for needle in [
        "const POLL_MS",
        "function pollFetch(url, options)",
        "AbortSignal.timeout(POLL_MS)",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // Every issued request is bounded: the only bare `fetch(` in the
    // page is the one inside pollFetch itself — the role and snapshot
    // polls, the history and journal since-reads, the command and
    // switch posts, and the signal index all go through it.
    assert_eq!(
        page.matches("fetch(").count(),
        1,
        "a request bypasses the bounded fetch"
    );
    assert!(
        page.matches("pollFetch(").count() >= 10,
        "expected the polls and posts to ride pollFetch"
    );
    // The failed poll's named state: the standing publication takes
    // the stale mark rather than the line hiding on stale
    // bookkeeping, and the failed stream reads name themselves.
    for needle in [
        "feed.stale = feed.publication",
        "stale publication",
        "history read failed",
        "journal read failed",
    ] {
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
    // and the old "anything unmatched is false" tests are absent.
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
