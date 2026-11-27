"""Optional JobSpy discovery for supported sources; application is separate."""
import argparse
from dataclasses import asdict
import math
import jobpilot


def text(value):
    if value is None or isinstance(value, float) and math.isnan(value): return ""
    return str(value)


def discover(args):
    unsupported = set(args.sites) - {'indeed', 'naukri'}
    if unsupported:
        raise ValueError(f"Automated discovery is disabled for: {', '.join(sorted(unsupported))}")
    from jobspy import scrape_jobs
    db = jobpilot.connect()
    try:
        for site in args.sites:
            try:
                jobs = scrape_jobs(site_name=[site], search_term=args.keywords, location="India", country_indeed="India",
                                   is_remote=True, hours_old=jobpilot.load_json('eligibility.json').get('maxPostingHours'), results_wanted=args.limit, fetch_description=True)
                imported = 0
                for row in jobs.to_dict("records"):
                    url = text(row.get("job_url_direct")) or text(row.get("job_url"))
                    if not url.startswith("https://"): continue
                    location = text(row.get("location"))
                    if row.get("is_remote") is True: location = "Remote " + location
                    # Day-only dates do not prove a rolling 24-hour posting time.
                    posted = text(row.get("date_posted"))
                    job = jobpilot.Job(text(row.get("id")) or url, text(row.get("company")), text(row.get("title")),
                                       text(row.get("description")), location, site, text(row.get("job_url")), url,
                                       jobpilot.ats_for(url), posted)
                    params = asdict(job) | {"discovered_at":jobpilot.now(), "fingerprint":job.fingerprint, "description_hash":job.description_hash}
                    db.execute("""INSERT OR IGNORE INTO jobs(external_id,company,title,description,location,source,source_url,apply_url,ats,posted_at,discovered_at,fingerprint,description_hash)
                        VALUES(:external_id,:company,:title,:description,:location,:source,:source_url,:apply_url,:ats,:posted_at,:discovered_at,:fingerprint,:description_hash)""", params)
                    db.execute("UPDATE jobs SET description=?,description_hash=?,posted_at=? WHERE apply_url=?", (job.description,job.description_hash,posted,url))
                    imported += 1
                db.commit()
                print(f"{site}: processed {imported} listings", flush=True)
            except Exception as exc:
                print(f"{site}: {type(exc).__name__}: {exc}", flush=True)
    finally: db.close()
    jobpilot.cmd_score(args)
    jobpilot.cmd_queue(args)
    jobpilot.cmd_export(args)


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--sites", nargs="+", choices=["indeed","naukri"], default=["indeed","naukri"])
    parser.add_argument("--keywords", default=".NET C# developer")
    parser.add_argument("--limit", type=int, default=20)
    discover(parser.parse_args())
