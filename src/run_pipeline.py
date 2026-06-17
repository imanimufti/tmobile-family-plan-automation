#!/usr/bin/env python3
"""
Unattended pipeline orchestrator.

Runs one idempotent pass of the full monthly cycle. Designed to be fired a few
times a day by a launchd agent. Stages (each gated so nothing repeats):

  1. ACQUIRE  - ensure this month's bill PDF is in bills/ (fetch if missing).
  2. PROCESS  - parse the PDF and build the month's Sheet tab (exactly once).
  3. ANNOUNCE - post the breakdown to WhatsApp (only when the GUI is unlocked).
  4. MONITOR  - run a payment-matching pass (always; works while locked).

Graceful degradation: if the T-Mobile fetch fails or the Mac is locked, the
stage is deferred and a macOS notification tells you the single manual action
needed. Re-runs are safe — per-month state lives in state/pipeline_state.json.

Run `python3 src/run_pipeline.py --dry-run` to see what each stage would do.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

# Run everything relative to the project root, like the other scripts expect.
ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "src"))

STATE_PATH = ROOT / "state" / "pipeline_state.json"
LOG_DIR = ROOT / "logs"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> Dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: Dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def month_state(state: Dict, tab: str) -> Dict:
    return state.setdefault(tab, {"pdf_fetched": False, "sheet_done": False, "announced": False})


# ---------------------------------------------------------------------------
# macOS helpers
# ---------------------------------------------------------------------------

def notify(title: str, message: str) -> None:
    """Best-effort macOS banner so you learn about a needed manual step."""
    safe = message.replace('"', "'")
    safe_title = title.replace('"', "'")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe}" with title "{safe_title}"'],
            check=False, capture_output=True,
        )
    except Exception:
        pass


def session_is_active() -> bool:
    """True only when a user is logged in at the console and the screen is
    unlocked — the condition required to drive WhatsApp via keystrokes.
    Unknown/undeterminable is treated as inactive (defer rather than misfire)."""
    try:
        import Quartz
        d = Quartz.CGSessionCopyCurrentDictionary()
        if not d:
            return False
        locked = bool(d.get("CGSSessionScreenIsLocked", 0))
        on_console = bool(d.get("kCGSSessionOnConsoleKey", 0))
        return on_console and not locked
    except Exception as e:
        print(f"[announce] Could not determine session state ({e}); treating as locked")
        return False


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def current_tab(when: Optional[datetime] = None) -> str:
    return (when or datetime.now()).strftime("%b %y")  # e.g. "Jun 26"


def current_pdf_path(when: Optional[datetime] = None) -> Path:
    return ROOT / "bills" / f"SummaryBill{(when or datetime.now()).strftime('%b%Y')}.pdf"


def stage_acquire(state: Dict, tab: str, dry_run: bool) -> Optional[Path]:
    """Ensure the current month's PDF exists; fetch it (≤ once/day) if missing."""
    ms = month_state(state, tab)
    pdf = current_pdf_path()
    if pdf.exists():
        ms["pdf_fetched"] = True
        print(f"[acquire] PDF present: {pdf.name}")
        return pdf

    today = datetime.now().strftime("%Y-%m-%d")
    fetch_meta = state.setdefault("_fetch", {})
    if fetch_meta.get("last_attempt") == today:
        print("[acquire] PDF missing; already attempted a fetch today — skipping")
        return None

    import fetch_tmobile_bill
    with open("src/config.json") as f:
        cfg = json.load(f)
    svc = cfg.get("tmobile", {}).get("keychain_service", "tmobile-login")

    # Don't attempt (or nag) until the password is configured in the Keychain —
    # that's a one-time manual setup step, not a failure worth a notification.
    if not fetch_tmobile_bill._keychain_secret(svc, "password"):
        print("[acquire] PDF missing; T-Mobile password not in Keychain yet — "
              "skipping fetch (setup incomplete)")
        return None

    if dry_run:
        print(f"[acquire] PDF missing; WOULD attempt T-Mobile fetch -> {pdf.name}")
        return None

    print("[acquire] PDF missing; attempting T-Mobile fetch...")
    fetch_meta["last_attempt"] = today
    save_state(state)
    try:
        # fetch_bill names the file by the bill's real statement month and skips
        # download if we already have it — so it returns the current month's path
        # only when T-Mobile has actually issued this month's bill.
        out = fetch_tmobile_bill.fetch_bill(cfg, cfg.get("sms", {}))
        if out and out.resolve() == pdf.resolve():
            ms["pdf_fetched"] = True
            print(f"[acquire] Fetched current month's bill: {out.name}")
            return out
        if out:
            print(f"[acquire] Fetched a bill for another month ({out.name}); "
                  "not the current month — leaving it for its own cycle")
        else:
            print("[acquire] No new bill from T-Mobile yet (current bill already on file)")
    except Exception as e:
        print(f"[acquire] Fetch failed: {e}")
        notify("T-Mobile bill", f"Couldn't auto-fetch this month's bill. Drop the PDF in bills/ as {pdf.name}.")
    return None


def stage_process(state: Dict, tab: str, pdf: Optional[Path], dry_run: bool) -> None:
    """Parse the PDF and build the Sheet tab — exactly once per month.

    Guarded hard: update_sheet() OVERWRITES the tab (resetting payment statuses),
    so we never run it if state says done OR the tab already exists in the sheet.
    """
    ms = month_state(state, tab)
    if ms.get("sheet_done"):
        print("[process] Sheet already built for this month — skipping")
        return
    if not pdf or not pdf.exists():
        print("[process] No PDF yet — skipping")
        return

    if dry_run:
        print(f"[process] WOULD parse {pdf.name} and build/refresh tab '{tab}'")
        return

    from parse_tmobile_bill import TMobileBillParser
    from update_google_sheet import GoogleSheetsUpdater

    updater = GoogleSheetsUpdater()
    updater.authenticate("credentials.json")

    if updater.get_sheet_id(tab) is not None:
        # Tab exists already (e.g. built manually). Do NOT overwrite and clobber
        # any recorded payments — just record it as done.
        print(f"[process] Tab '{tab}' already exists — marking done without overwrite")
        ms["sheet_done"] = True
        save_state(state)
        return

    print(f"[process] Parsing {pdf.name} and building tab '{tab}'...")
    bill_data = TMobileBillParser(str(pdf)).parse()
    updater.update_sheet(bill_data, tab)
    ms["sheet_done"] = True
    save_state(state)
    print(f"[process] Built tab '{tab}'")


def stage_announce(state: Dict, tab: str, dry_run: bool) -> None:
    """Post the breakdown to WhatsApp once via headless WhatsApp Web — works even
    while the Mac is locked (unlike the WhatsApp Desktop keystroke path)."""
    ms = month_state(state, tab)
    if not ms.get("sheet_done"):
        print("[announce] Sheet not built yet — skipping")
        return
    if ms.get("announced"):
        print("[announce] Already announced this month — skipping")
        return

    if dry_run:
        print(f"[announce] WOULD send WhatsApp breakdown for '{tab}' via WhatsApp Web")
        return

    print(f"[announce] Sending WhatsApp breakdown for '{tab}' via WhatsApp Web...")
    result = subprocess.run(
        [sys.executable, "src/share_to_whatsapp.py", tab, "--send-mode", "web"],
        capture_output=True, text=True,
    )
    sys.stdout.write(result.stdout)
    if result.returncode == 0:
        ms["announced"] = True
        ms["announced_at"] = datetime.now().isoformat()  # anchors the reminder clock
        save_state(state)
        print("[announce] Sent")
    else:
        sys.stdout.write(result.stderr)
        print("[announce] Send failed — will retry next run")
        notify("T-Mobile bill", f"WhatsApp send for {tab} failed; will retry.")


def stage_monitor(tab: str, days_back: int, dry_run: bool):
    """Run a single payment-matching pass. Unaffected by lock state.
    Returns (monitor, target_tab) so the reminder stage can reuse the session."""
    from monitor_venmo_payments import PaymentMonitor
    monitor = PaymentMonitor()
    monitor.authenticate("credentials.json")
    target = monitor.detect_current_tab() or tab
    print(f"[monitor] Pass over '{target}' (recent {monitor.match_months} month(s))")
    monitor.process_payments(target, days_back=days_back,
                             source_selection="all", dry_run=dry_run)
    return monitor, target


def _tab_pdf_issue_date(tab: str) -> Optional[datetime]:
    """Parse the 'Bill issue date' from the tab's bill PDF, as a fallback
    reference for the reminder clock when announced_at isn't recorded."""
    m = re.match(r'([A-Za-z]{3})\s+(\d{2})$', tab)
    if not m:
        return None
    pdf = ROOT / "bills" / f"SummaryBill{m.group(1).capitalize()}20{m.group(2)}.pdf"
    if not pdf.exists():
        return None
    try:
        import pymupdf
        doc = pymupdf.open(str(pdf))
        text = doc[0].get_text()
        doc.close()
        d = re.search(r'Bill issue date\s+([A-Za-z]{3,9})\s+(\d{1,2}),\s*(\d{4})', text)
        if d:
            return datetime.strptime(f"{d.group(1)[:3]} {d.group(2)} {d.group(3)}", "%b %d %Y")
    except Exception:
        return None
    return None


def stage_remind(monitor, state: Dict, tab: str, dry_run: bool) -> None:
    """DM each person still unpaid past the reminder thresholds. Respects the
    couples rule: a dependent's (e.g. Tuba's) reminder is rolled into the
    responsible payer's (Qasim's) DM, since the payer covers them."""
    with open("src/config.json") as f:
        cfg = json.load(f)
    rcfg = cfg.get("reminders", {})
    if not rcfg.get("enabled"):
        print("[remind] Reminders disabled in config — skipping")
        return

    numbers = rcfg.get("whatsapp_numbers", {})
    first_after = int(rcfg.get("first_after_days", 7))
    repeat_every = int(rcfg.get("repeat_every_days", 5))

    ms = month_state(state, tab)
    ref_iso = ms.get("announced_at")
    ref = datetime.fromisoformat(ref_iso) if ref_iso else _tab_pdf_issue_date(tab)
    if not ref:
        print("[remind] No reference date (announce/issue) for this bill — skipping")
        return
    days = (datetime.now() - ref).days
    if days < first_after:
        print(f"[remind] Bill is {days} day(s) old (< {first_after}) — no reminders yet")
        return

    # Roll each pending charge up to the person responsible for paying it.
    responsible = {dep: payer for payer, deps in cfg.get("pays_for", {}).items() for dep in deps}
    skip_names = {"Imani", "Imani Internet", "Unused Line"}
    owed: Dict[str, float] = {}
    for p in monitor.get_sheet_data(tab):
        if p["payment_status"].lower() == "paid" or p["total"] <= 0 or p["name"] in skip_names:
            continue
        payer = responsible.get(p["name"], p["name"])
        owed[payer] = owed.get(payer, 0.0) + p["total"]

    if not owed:
        print("[remind] Everyone has paid — no reminders due")
        return

    # Build payment-method lines for the DM body.
    methods = cfg.get("whatsapp", {}).get("payment_methods", {})
    methods_str = "\n".join(f"• {k}: {v}" for k, v in methods.items())
    template = rcfg.get("dm_template", "Reminder: your T-Mobile share for {tab} is ${amount}.")

    rstate = ms.setdefault("reminders", {})
    now = datetime.now()
    batch = []
    for payer, amount in sorted(owed.items()):
        num = numbers.get(payer)
        if not num:
            print(f"[remind] No WhatsApp number for {payer} — skipping")
            continue
        last_iso = rstate.get(payer)
        if last_iso:
            if (now - datetime.fromisoformat(last_iso)).days < repeat_every:
                continue  # reminded recently
        msg = template.format(name=payer, tab=tab, amount=f"{amount:.2f}", methods=methods_str)
        batch.append({"to": num, "message": msg, "payer": payer})

    if not batch:
        print("[remind] No reminders due this run (all reminded recently)")
        return

    if dry_run:
        print(f"[remind] WOULD DM {len(batch)} person(s) for '{tab}' ({days} days old):")
        for b in batch:
            print(f"    → {b['payer']} ({b['to']})")
        return

    payload = [{"to": b["to"], "message": b["message"]} for b in batch]
    batch_file = ROOT / "state" / "reminder_batch.json"
    batch_file.write_text(json.dumps(payload))
    node = _node_bin()
    print(f"[remind] DMing {len(batch)} person(s) for '{tab}'...")
    result = subprocess.run(
        [node, str(ROOT / "whatsapp" / "send.js"), "send-batch", "--file", str(batch_file)],
        capture_output=True, text=True,
    )
    sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stdout.write(result.stderr)
    # Record reminders for the numbers confirmed sent.
    sent_nums = set(re.findall(r'DM sent to (\d+)', result.stdout))
    for b in batch:
        if re.sub(r"\D", "", b["to"]) in sent_nums:
            rstate[b["payer"]] = now.isoformat()
    save_state(state)
    batch_file.unlink(missing_ok=True)


def _node_bin() -> str:
    import shutil
    return shutil.which("node") or next(
        (p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node") if Path(p).exists()),
        "node")


def main():
    parser = argparse.ArgumentParser(description="Unattended T-Mobile billing pipeline")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what each stage would do; change nothing")
    parser.add_argument("--days", type=int, default=7,
                        help="Payment look-back window for the monitor (default 7)")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Never attempt the T-Mobile download this run")
    parser.add_argument("--remind-tab", default=None,
                        help="Run ONLY the reminder stage for this tab (e.g. 'Apr 26'), then exit")
    args = parser.parse_args()

    # Ad-hoc: send reminders for a specific past month, then exit.
    if args.remind_tab:
        from monitor_venmo_payments import PaymentMonitor
        print(f"Reminder-only run for '{args.remind_tab}'"
              f"{' [dry-run]' if args.dry_run else ''}")
        monitor = PaymentMonitor()
        monitor.authenticate("credentials.json")
        st = load_state()
        stage_remind(monitor, st, args.remind_tab, args.dry_run)
        return

    print("=" * 64)
    print(f"Pipeline run {datetime.now().isoformat(timespec='seconds')}"
          f"{' [dry-run]' if args.dry_run else ''}")
    print(f"Interpreter: {sys.executable}")  # the binary to grant FDA/Accessibility
    print("=" * 64)

    state = load_state()
    tab = current_tab()

    try:
        pdf = None
        if args.skip_fetch:
            p = current_pdf_path()
            pdf = p if p.exists() else None
            month_state(state, tab)["pdf_fetched"] = bool(pdf)
        else:
            pdf = stage_acquire(state, tab, args.dry_run)

        stage_process(state, tab, pdf, args.dry_run)
        stage_announce(state, tab, args.dry_run)
        if not args.dry_run:
            save_state(state)
    except Exception as e:
        print(f"[pipeline] Bill stages errored: {e}")
        import traceback
        traceback.print_exc()

    # Monitor always runs, even if earlier stages failed; reminders follow it
    # (so freshly-cleared payments aren't reminded).
    try:
        monitor, target = stage_monitor(tab, args.days, args.dry_run)
        stage_remind(monitor, state, target, args.dry_run)
    except Exception as e:
        print(f"[monitor/remind] Errored: {e}")
        import traceback
        traceback.print_exc()

    print(f"\nPipeline run complete {datetime.now().isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
