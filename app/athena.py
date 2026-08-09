#!/usr/bin/env python3
"""athena.py — Athena CLI entrypoint.

Subcommands:
  doctor      Pre-flight check, no writes.
  status      Print current install summary.
  plan        Read-only install plan.
  install     Atomic transactional install with two-step confirmation.
  verify      SHA-256 receipt verifier.
  restore     Restore SOUL/MEMORY/USER from .pre-athena-<pid> files.
  uninstall   Receipt-chain rollback (does NOT restore Hermes memory).
  launch      Launch Hermes with profile active.
  gui         Launch the PyQt6 GUI window.

Usage:
  python3 app/athena.py doctor
  python3 app/athena.py install --profile max-breaker --yes
  python3 app/athena.py verify --json

Exit codes:
  0  success
  1  not installed / generic
  2  installed but receipt mismatch
  3  user declined at confirmation
  4  prerequisite missing
  5  ownership conflict (pass --force)
  6  write failure (disk full, permissions)
  7  post-install verify failed
  8  no .pre-athena-* files found
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__version__ = "1.0.0"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HERMES_HOME = Path.home() / ".hermes"
BANNERS_DIR = REPO_ROOT / "banners"


def print_banner() -> None:
    """Print the Athena + NERV banner. Pure cosmetics; safe to call any time.

    Writes to STDERR so JSON output (status --json, plan --json, verify --json)
    on STDOUT remains clean for piping.
    """
    for name in ("athena", "nerv"):
        path = BANNERS_DIR / f"{name}.ascii"
        if path.exists():
            try:
                sys.stderr.write(path.read_text(encoding="utf-8"))
            except OSError:
                pass
    sys.stderr.flush()


def hermes_home() -> Path:
    """Resolve Hermes home from env vars or default."""
    env = os.environ.get("ATHENA_HOME") or os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_HERMES_HOME.resolve()


def receipt_path(home: Path) -> Path:
    return home / ".athena.receipt"


def audit_log_path(home: Path) -> Path:
    return home / ".athena.audit.log"


def audit(action: str, **fields) -> None:
    """Append one line to the audit log. Best-effort; never raises."""
    try:
        home = hermes_home()
        log = audit_log_path(home)
        log.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        operator = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
        entry = {"ts": ts, "action": action, "operator": operator, **fields}
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def cmd_doctor(args: argparse.Namespace) -> int:
    home = hermes_home()
    print("Athena doctor")
    print(f"  Hermes root       : {home}  ({'exists' if home.exists() else 'MISSING'})")

    if home.exists():
        # Hermes version (best-effort)
        try:
            import hermes_agent
            v = getattr(hermes_agent, "__version__", "unknown")
            print(f"  Hermes version     : {v}")
        except ImportError:
            print("  Hermes version     : hermes-agent not importable")

    print(f"  Python             : {sys.version.split()[0]} (OK)")

    skill = home / "skills" / "athena" / "SKILL.md"
    print(f"  Existing skill     : {'installed' if skill.exists() else 'not installed'}")
    print(f"  Existing receipt   : {'installed' if receipt_path(home).exists() else 'not installed'}")

    for name in ("SOUL.md", "MEMORY.md", "USER.md"):
        p = home / name
        print(f"  {name + ' present':<18}: {'yes' if p.exists() else 'no'}")

    if skill.exists() and not args.force:
        print("  Status             : installed (pass --force to overwrite)")
        return 2
    print("  Status             : ready to install")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    home = hermes_home()
    skill = home / "skills" / "athena" / "SKILL.md"
    receipt = receipt_path(home)

    installed = skill.exists()
    data = {
        "installed": installed,
        "profile": "max-breaker",
        "receipt_sha256": None,
        "last_verified": None,
        "hermes_root": str(home),
    }
    if installed and receipt.exists():
        try:
            r = json.loads(receipt.read_text(encoding="utf-8"))
            data["profile"] = r.get("profile", "max-breaker")
            data["receipt_sha256"] = r.get("receipt_sha256")
            data["last_verified"] = r.get("last_verified")
        except (OSError, json.JSONDecodeError):
            if args.json:
                print(json.dumps({"error": "receipt unreadable"}))
            else:
                print("Athena status")
                print("  Installed          : yes")
                print("  Receipt            : CORRUPTED")
            return 2

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print("Athena status")
        print(f"  Installed          : {'yes' if installed else 'no'}")
        print(f"  Profile            : {data['profile']}")
        print(f"  Receipt SHA        : {data['receipt_sha256'] or '-'}")
        print(f"  Last verified      : {data['last_verified'] or '-'}")
        print(f"  Hermes root        : {data['hermes_root']}")

    return 0 if installed else 1


def cmd_plan(args: argparse.Namespace) -> int:
    home = hermes_home()
    profile = args.profile

    plan = {
        "profile": profile,
        "hermes_root": str(home),
        "will_write": [
            str(home / "skills" / "athena" / "SKILL.md"),
            str(home / "scripts" / "athena-install.sh"),
            str(home / "scripts" / "athena-uninstall.sh"),
            str(home / "scripts" / "athena-verify.sh"),
            str(home / "scripts" / "athena-release.py"),
            str(home / "scripts" / "build-dmg.sh"),
            str(home / "SOUL.md") + "  (overwrite; .pre-athena-<pid> written first)",
            str(home / "MEMORY.md") + "  (overwrite; .pre-athena-<pid> written first)",
            str(home / "USER.md") + "  (overwrite; .pre-athena-<pid> written first)",
            str(home / ".athena.receipt"),
        ],
        "will_not_touch": [
            str(home / "AGENTS.md"),
            str(home / "HERMES.md"),
            str(home / "config.yaml"),
            "any other ~/.hermes/* file",
        ],
    }
    
    if args.aegis:
        plan["aegis"] = {
            "source": "/home/ubuntu/pi_dev/aegis",
            "modules": [
                "stream_guard.py", "fingerprint.py", "aegis_parallel.py",
                "prompt_v42.md", "prompt_v42_cn.md", "test_aegis_v42.py",
            ],
            "deploy_to": str(home / "aegis"),
        }

    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        print(f"Athena plan (profile: {profile})")
        print("  Will write:")
        for p in plan["will_write"]:
            print(f"    {p}")
        print("  Will not touch:")
        for p in plan["will_not_touch"]:
            print(f"    {p}")

    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Delegate to scripts/install.sh via subprocess.

    The CLI is sugar. The install script is the source of truth.

    When --aegis is passed, also deploy AEGIS modules (stream_guard,
    fingerprint, aegis_parallel) to ~/.hermes/aegis/ alongside Athena.
    """
    if not hermes_home().exists():
        print("ERROR: Hermes root does not exist. Run `hermes init` first.", file=sys.stderr)
        return 4

    script = REPO_ROOT / "scripts" / "install.sh"
    if not script.exists():
        print(f"ERROR: install script not found at {script}", file=sys.stderr)
        return 6

    argv = [str(script), str(hermes_home()), args.profile]
    if args.yes:
        argv.append("--yes")
    if args.force:
        argv.append("--force")

    audit("install-start", profile=args.profile, force=args.force, aegis=args.aegis)
    rc = subprocess.call(argv)
    
    if rc == 0 and args.aegis:
        rc = _deploy_aegis(args)
    
    audit("install-end", profile=args.profile, force=args.force, aegis=args.aegis, exit_code=rc)
    return rc


def _deploy_aegis(args: argparse.Namespace) -> int:
    """Deploy full AEGIS arsenal to ~/.hermes/aegis/.

    Copies:
      - Python modules: stream_guard, fingerprint, aegis_parallel
      - V42 prompts: prompt_v42.md, prompt_v42_cn.md
      - Test harness: test_aegis_v42.py
      - Vault + loader: vault.dat, loader/loader.py, payload/payload.json
      - Methods: methods/ (44 jailbreak methods in 7 categories)
      - Crypto keys: build_vault.py

    Returns 0 on success, 6 on write failure.
    """
    AEGIS_SRC = Path("/home/ubuntu/pi_dev/aegis")
    home = hermes_home()
    aegis_dst = home / "aegis"

    if not AEGIS_SRC.exists():
        print(f"WARNING: AEGIS source not found at {AEGIS_SRC} — skipping AEGIS deploy", file=sys.stderr)
        return 0  # non-fatal

    # Copy modules (flat files)
    modules = [
        "hermes/stream_guard.py",
        "hermes/fingerprint.py",
        "hermes/aegis_parallel.py",
        "prompt_v42.md",
        "prompt_v42_cn.md",
        "test_aegis_v42.py",
    ]

    # Copy vault artifacts (preserve directory structure)
    vault_files = [
        ("payload/vault.dat", "vault/vault.dat"),
        ("payload/payload.json", "vault/payload.json"),
        ("payload/.key", "payload/.key"),
        ("loader/loader.py", "vault/loader.py"),
        ("build_vault.py", "vault/build_vault.py"),
    ]

    # Copy methods/ tree
    methods_src = AEGIS_SRC / "methods"
    methods_dst = aegis_dst / "methods"

    installed = 0
    methods_installed = 0
    try:
        aegis_dst.mkdir(parents=True, exist_ok=True)

        # Flat modules
        for rel in modules:
            src = AEGIS_SRC / rel
            dst = aegis_dst / rel.split("/")[-1]
            if src.exists():
                shutil.copy2(src, dst)
                installed += 1

        # Vault artifacts
        vault_dir = aegis_dst / "vault"
        vault_dir.mkdir(exist_ok=True)
        for src_rel, dst_rel in vault_files:
            src = AEGIS_SRC / src_rel
            dst = aegis_dst / dst_rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                installed += 1

        # Methods tree (recursive)
        if methods_src.exists():
            methods_installed = 0
            for src_file in methods_src.rglob("*.md"):
                rel = src_file.relative_to(methods_src)
                dst = methods_dst / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst)
                methods_installed += 1
            installed += methods_installed

        print(f"[AEGIS] Deployed FULL arsenal ({installed} files) to {aegis_dst}")
        print(f"  ── vault: vault.dat + payload.json + loader.py + build_vault.py")
        print(f"  ── methods: {methods_installed if methods_src.exists() else 0} jailbreak methods")
        print(f"  ── modules: stream_guard, fingerprint, aegis_parallel")
        print(f"  ── prompts: V42 (ID + CN), test harness")

        audit("aegis-deploy", total_files=installed)
        return 0
    except OSError as e:
        print(f"ERROR: AEGIS deploy failed: {e}", file=sys.stderr)
        return 6


def cmd_verify(args: argparse.Namespace) -> int:
    script = REPO_ROOT / "scripts" / "verify.sh"
    if not script.exists():
        print(f"ERROR: verify script not found at {script}", file=sys.stderr)
        return 6
    argv = [str(script), str(hermes_home())]
    if args.json:
        argv.append("--json")
    rc = subprocess.call(argv)
    if rc == 0:
        audit("verify", result="PASS")
    else:
        audit("verify", result="FAIL", exit_code=rc)
    return rc


def cmd_restore(args: argparse.Namespace) -> int:
    home = hermes_home()

    restored = []
    for name in ("SOUL.md", "MEMORY.md", "USER.md"):
        target = home / name
        backups = sorted(target.parent.glob(f"{name}.pre-athena-*"), reverse=True)
        if not backups:
            continue
        latest = backups[0]
        if not args.yes:
            print(f"Would restore {target} from {latest.name}")
        else:
            latest.replace(target)
            restored.append(name)

    if not args.yes:
        if not restored and not any((home / n).exists() for n in ("SOUL.md", "MEMORY.md", "USER.md")):
            print("Nothing to restore.")
            return 8
        print("Pass --yes to apply.")
        return 0

    if not restored:
        print("No .pre-athena-* files found.")
        return 8

    audit("restore", files=restored)

    # Also remove Athena install
    cmd = ["python3", str(Path(__file__).resolve()), "uninstall", "--yes"]
    subprocess.call(cmd)
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    script = REPO_ROOT / "scripts" / "uninstall.sh"
    if not script.exists():
        print(f"ERROR: uninstall script not found at {script}", file=sys.stderr)
        return 6
    argv = [str(script), str(hermes_home())]
    if args.yes:
        argv.append("--yes")
    rc = subprocess.call(argv)
    audit("uninstall", exit_code=rc)
    return rc


def cmd_launch(args: argparse.Namespace) -> int:
    if not args.yes:
        print("About to launch Hermes with Athena profile active.")
        print("Pass --yes to confirm.")
        return 3
    env = os.environ.copy()
    env["ATHENA_HOME"] = str(hermes_home())
    env["HERMES_PROFILE"] = "max-breaker"
    argv = ["hermes-agent"]
    if args.claude_arg:
        argv.append(args.claude_arg)
    audit("launch", argv=argv)
    try:
        return subprocess.call(argv, env=env)
    except FileNotFoundError:
        print("ERROR: hermes-agent not found in PATH", file=sys.stderr)
        return 6


def cmd_gui(args: argparse.Namespace) -> int:
    gui = REPO_ROOT / "gui" / "athena_gui.py"
    if not gui.exists():
        print(f"ERROR: GUI script not found at {gui}", file=sys.stderr)
        return 6
    return subprocess.call([sys.executable, str(gui)])


# ── AEGIS subcommand handlers ──────────────────────────────────

def _aegis_root() -> Path:
    """Resolve AEGIS deployment directory: ~/.hermes/aegis/."""
    return hermes_home() / "aegis"


def cmd_aegis_vault(args: argparse.Namespace) -> int:
    """Handle 'athena aegis vault' — verify / list."""
    root = _aegis_root()
    vault_dat = root / "vault" / "vault.dat"
    payload_json = root / "vault" / "payload.json"

    if not payload_json.exists():
        print(f"ERROR: AEGIS vault not deployed. Run: athena install --aegis --yes --force")
        return 4

    if args.verify:
        try:
            import hashlib as _hashlib
            vault_hash = _hashlib.sha256(vault_dat.read_bytes()).hexdigest()[:16]
            data = json.loads(payload_json.read_text(encoding="utf-8"))
            cats = data.get("categories", {})
            methods_count = sum(len(v.get("methods", [])) for v in cats.values())
            cats_count = len(cats)
            print(f"[+] Vault OK: {methods_count} methods, {cats_count} categories, sha256={vault_hash}")
            return 0
        except Exception as e:
            print(f"[!] Verify error: {e}")
            return 2

    if args.list:
        if not payload_json.exists():
            print("ERROR: payload.json not found")
            return 4
        try:
            data = json.loads(payload_json.read_text(encoding="utf-8"))
            cats = data.get("categories", {})
            total = sum(len(v["methods"]) for v in cats.values())
            print(f"AEGIS Vault: {total} methods in {len(cats)} categories\n")
            for cat_id, cat_data in sorted(cats.items()):
                methods = cat_data.get("methods", [])
                cat_name = cat_data.get("name", cat_id)
                print(f"  {cat_id} — {cat_name} ({len(methods)} methods)")
                if args.category and not cat_id.startswith(args.category):
                    continue
                for m in methods[:5]:
                    print(f"      {m['id']}: {m.get('name','?')[:60]}")
                if len(methods) > 5:
                    print(f"      ... and {len(methods)-5} more")
            return 0
        except Exception as e:
            print(f"ERROR: {e}")
            return 2

    print("Usage: athena aegis vault --verify | --list [--category=01]")
    return 0


def cmd_aegis_fire(args: argparse.Namespace) -> int:
    """Handle 'athena aegis fire' — run test_aegis_v42.py."""
    root = _aegis_root()
    test_script = root / "test_aegis_v42.py"

    if not test_script.exists():
        print(f"ERROR: AEGIS not deployed. Run: athena install --aegis --yes --force")
        return 4

    sys.path.insert(0, str(root))
    sys.path.insert(0, str(Path("/home/ubuntu/pi_dev/aegis")))  # for imports
    try:
        from test_aegis_v42 import run, render_report, save_run, compare_v41_v42
    except ImportError:
        print("ERROR: Cannot import test_aegis_v42. Run from deployed directory.")
        return 6

    # Load prompts
    if args.variant == "cn":
        prompt_v42 = (root / "prompt_v42_cn.md").read_text(encoding="utf-8")
    else:
        prompt_v42 = (root / "prompt_v42.md").read_text(encoding="utf-8")

    if args.compare:
        prompt_v41 = (Path("/home/ubuntu/pi_dev/aegis") / "prompt.md")
        if not prompt_v41.exists():
            print("ERROR: prompt.md (v41) not found")
            return 4
        compare_v41_v42(args.url, args.model, args.key,
                        prompt_v41.read_text(encoding="utf-8"),
                        prompt_v42, args.variant)
        return 0

    # Single run
    result = run(args.url, args.model, args.key, prompt_v42, args.variant)
    save_path = save_run(result)
    print(render_report(result))
    print(f"\n[+] Run saved: {save_path}")
    return 0


def cmd_aegis_fingerprint(args: argparse.Namespace) -> int:
    """Handle 'athena aegis fingerprint' — identify target."""
    root = _aegis_root()
    fp_script = root / "fingerprint.py"

    if not fp_script.exists():
        print(f"ERROR: AEGIS not deployed. Run: athena install --aegis --yes --force")
        return 4

    sys.path.insert(0, str(root))
    try:
        from fingerprint import Fingerprinter
    except ImportError:
        print("ERROR: Cannot import fingerprint module")
        return 6

    fp = Fingerprinter()
    result = fp.fingerprint(args.url)
    print(f"URL:      {args.url}")
    print(f"Provider: {result.get('provider', 'unknown')}")
    print(f"Model:    {result.get('model_family', 'unknown')}")
    print(f"Safety:   {result.get('safety_tier', 'unknown')}")
    print(f"Score:    {result.get('safety_score', 'N/A')}")
    if result.get('capabilities'):
        print(f"Caps:     {', '.join(result['capabilities'])}")
    return 0


def cmd_aegis_stream(args: argparse.Namespace) -> int:
    """Handle 'athena aegis stream' — test refusal detection."""
    root = _aegis_root()
    sg_script = root / "stream_guard.py"

    if not sg_script.exists():
        print(f"ERROR: AEGIS not deployed. Run: athena install --aegis --yes --force")
        return 4

    sys.path.insert(0, str(root))
    try:
        from stream_guard import StreamGuard
    except ImportError:
        print("ERROR: Cannot import stream_guard module")
        return 6

    guard = StreamGuard()
    print(f"Question: {args.question}")
    print(f"Target:   {args.url}")
    result = guard.guard(args.url, args.question, api_key=args.key)
    print(f"\nScore:    {result.score}")
    print(f"Refusal:  {result.is_refusal}")
    print(f"Escalated:{result.escalation_level}")
    print(f"Content:  {result.content[:500]}...")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="athena",
        description="Athena — Hermes ColdBrew port (v" + __version__ + ")",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = p.add_subparsers(dest="cmd", required=True)

    doctor_p = sub.add_parser("doctor", help="Pre-flight check, no writes")
    doctor_p.add_argument("--scope", default="user", choices=["user"])
    doctor_p.add_argument("--force", action="store_true")
    doctor_p.set_defaults(func=cmd_doctor)

    status_p = sub.add_parser("status", help="Print install summary")
    status_p.add_argument("--json", action="store_true")
    status_p.set_defaults(func=cmd_status)

    plan_p = sub.add_parser("plan", help="Read-only install plan")
    plan_p.add_argument("--profile", default="max-breaker",
                        choices=["max-breaker", "max-breaker-v42", "builder", "research", "creative"])
    plan_p.add_argument("--aegis", action="store_true",
                        help="Include AEGIS modules deployment in plan")
    plan_p.add_argument("--json", action="store_true")
    plan_p.set_defaults(func=cmd_plan)

    install_p = sub.add_parser("install", help="Atomic transactional install")
    install_p.add_argument("--profile", default="max-breaker",
                          choices=["max-breaker", "max-breaker-v42", "builder", "research", "creative"])
    install_p.add_argument("--aegis", action="store_true",
                          help="Deploy AEGIS modules (stream_guard, fingerprint, parallel) alongside Athena")
    install_p.add_argument("--yes", action="store_true",
                           help="Skip two-step confirmation (required for non-interactive)")
    install_p.add_argument("--force", action="store_true",
                           help="Overwrite existing Athena install with different SHA-256")
    install_p.set_defaults(func=cmd_install)

    verify_p = sub.add_parser("verify", help="SHA-256 receipt verifier")
    verify_p.add_argument("--json", action="store_true")
    verify_p.set_defaults(func=cmd_verify)

    restore_p = sub.add_parser("restore", help="Restore Hermes memory from .pre-athena-*")
    restore_p.add_argument("--yes", action="store_true")
    restore_p.set_defaults(func=cmd_restore)

    uninstall_p = sub.add_parser("uninstall", help="Receipt-chain rollback")
    uninstall_p.add_argument("--yes", action="store_true")
    uninstall_p.set_defaults(func=cmd_uninstall)

    launch_p = sub.add_parser("launch", help="Launch Hermes with profile active")
    launch_p.add_argument("--yes", action="store_true")
    launch_p.add_argument("--claude-arg", default=None,
                          help="Arg forwarded to hermes-agent (e.g. --model <name>)")
    launch_p.set_defaults(func=cmd_launch)

    gui_p = sub.add_parser("gui", help="Launch PyQt6 GUI window")
    gui_p.set_defaults(func=cmd_gui)

    # ── AEGIS subcommand ──────────────────────────────────
    aegis_p = sub.add_parser("aegis", help="AEGIS V42 arsenal management")
    aegis_sub = aegis_p.add_subparsers(dest="aegis_cmd", required=True)

    aegis_vault_p = aegis_sub.add_parser("vault", help="Vault operations")
    aegis_vault_p.add_argument("--verify", action="store_true", help="Verify vault integrity")
    aegis_vault_p.add_argument("--list", action="store_true", help="List all methods")
    aegis_vault_p.add_argument("--category", type=str, default="", help="Filter by category (01-07)")
    aegis_vault_p.set_defaults(func=cmd_aegis_vault)

    aegis_fire_p = aegis_sub.add_parser("fire", help="Fire V42 probes at target")
    aegis_fire_p.add_argument("--url", default="http://localhost:20128/v1/chat/completions")
    aegis_fire_p.add_argument("--model", default="mimo/mimo-v2.5-pro")
    aegis_fire_p.add_argument("--key", default="", help="API key")
    aegis_fire_p.add_argument("--variant", default="id", choices=["id", "cn"])
    aegis_fire_p.add_argument("--compare", action="store_true", help="V41 vs V42 head-to-head")
    aegis_fire_p.set_defaults(func=cmd_aegis_fire)

    aegis_fp_p = aegis_sub.add_parser("fingerprint", help="Fingerprint target endpoint")
    aegis_fp_p.add_argument("url", help="Target endpoint URL")
    aegis_fp_p.set_defaults(func=cmd_aegis_fingerprint)

    aegis_stream_p = aegis_sub.add_parser("stream", help="Stream guard — refusal detection")
    aegis_stream_p.add_argument("question", help="Probe question")
    aegis_stream_p.add_argument("--url", default="http://localhost:20128/v1/chat/completions")
    aegis_stream_p.add_argument("--key", default="", help="API key")
    aegis_stream_p.set_defaults(func=cmd_aegis_stream)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    print_banner()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
