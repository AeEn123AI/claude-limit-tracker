#!/usr/bin/env python3
"""Fail if anything that is (or is about to be) published looks private.

Scans every text file in docs/ plus every tracked, staged and untracked-but-not-ignored
file in the git work tree, and the author/committer/message of every commit, for:
  * e-mail addresses (except GitHub/Anthropic noreply addresses, and only in commit metadata)
  * UUIDs (session / account / organization ids look like these)
  * home-directory paths
  * any extra terms listed one per line in local/deny.txt (or --deny FILE), case-insensitive,
    e.g. your name and account names. Keep that file out of git (local/ is ignored).

Exit status 1 if anything is found. Run it before every commit/push (run_daily.sh does).
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
HOME = re.compile("/" + "home/|/" + "Users/[A-Za-z]|[A-Za-z]:\\\\" + "Users\\\\")
ALLOWED_COMMIT_EMAILS = re.compile(r"(@users\.noreply\.github\.com|^noreply@anthropic\.com)$", re.I)
# placeholder addresses that may appear in docs/examples
ALLOWED_FILE_EMAILS = re.compile(r"@(example\.(com|org|net))$", re.I)


def git(*args):
    try:
        return subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def files_to_scan():
    names = set()
    for args in (["ls-files"], ["ls-files", "--others", "--exclude-standard"], ["diff", "--cached", "--name-only"]):
        names.update(x for x in git(*args).splitlines() if x)
    for dp, _, fs in os.walk(os.path.join(ROOT, "docs")):
        for f in fs:
            names.add(os.path.relpath(os.path.join(dp, f), ROOT))
    return sorted(n for n in names if os.path.isfile(os.path.join(ROOT, n)))


def is_binary(data):
    return b"\0" in data[:8192]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deny", default=os.path.join(ROOT, "local", "deny.txt"))
    a = ap.parse_args()
    deny = []
    if os.path.exists(a.deny):
        with open(a.deny, encoding="utf-8") as fh:
            deny = [x.strip() for x in fh if x.strip() and not x.startswith("#")]
    deny_re = re.compile("|".join(re.escape(d) for d in deny), re.I) if deny else None

    problems = []

    def check_text(where, text, emails_ok):
        for i, line in enumerate(text.splitlines(), 1):
            for m in EMAIL.finditer(line):
                if not emails_ok.search(m.group(0)):
                    problems.append("%s:%d: e-mail address" % (where, i))
            if UUID.search(line):
                problems.append("%s:%d: UUID" % (where, i))
            if HOME.search(line):
                problems.append("%s:%d: home directory path" % (where, i))
            if deny_re and deny_re.search(line):
                problems.append("%s:%d: denied term %r" % (where, i, deny_re.search(line).group(0)))

    files = files_to_scan()
    for name in files:
        with open(os.path.join(ROOT, name), "rb") as fh:
            data = fh.read()
        if is_binary(data):
            continue
        check_text(name, data.decode("utf-8", "replace"), ALLOWED_FILE_EMAILS)

    log = git("log", "--all", "--format=%H%n%an <%ae>%n%cn <%ce>%n%B%n--END--")
    commit = None
    for line in log.splitlines():
        if re.fullmatch(r"[0-9a-f]{40}", line):
            commit = line[:10]
            continue
        for m in EMAIL.finditer(line):
            if not ALLOWED_COMMIT_EMAILS.search(m.group(0)):
                problems.append("commit %s: e-mail address in author/committer/message" % commit)
        if UUID.search(line) or HOME.search(line):
            problems.append("commit %s: UUID or home path in message" % commit)
        # account names in author fields are expected; only the message is checked for denied terms
        if deny_re and deny_re.search(line) and not re.match(r".* <[^>]*>$", line):
            problems.append("commit %s: denied term in message" % commit)

    if problems:
        print("privacy check FAILED (%d findings):" % len(problems))
        for p in sorted(set(problems)):
            print("  " + p)
        return 1
    print("privacy check ok: %d files, %d commits, %d extra terms" % (len(files), log.count("--END--"), len(deny)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
