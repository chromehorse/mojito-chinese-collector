from scraper.adapters import listing, issue_archive, policy_db, journal_toc, seed_doc, policy_api

BY_KIND = {
    "listing": listing,
    "issue-archive": issue_archive,
    "policy-db": policy_db,
    "journal-toc": journal_toc,
    "seed-doc": seed_doc,
    "policy-api": policy_api,
}
