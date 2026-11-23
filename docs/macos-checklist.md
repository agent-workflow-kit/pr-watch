# macOS manual checklist

Run this once on a Mac before 1.0 (issue #32; [plan.md, Stage 6](plan.md#stage-6-cron-docs-and-live-suite), [Platform notes](interface.md#platform-notes)). CI is Linux only, so the macOS-specific paths (no `/proc`, no `os.waitid`, BSD `stat` and `ps`, vixie cron 4.x, `/bin/sh` being bash, Terminal and iTerm2 for `browse`) are covered by unit tests with fakes, or not at all. This checklist is how they meet a real macOS.

**Status: not run on macOS.** Nothing below was observed on a Mac. Every expected result is derived from the code and from [interface.md](interface.md) (the section is named in each step). Items marked **to be observed** state the invariant that must hold and leave the value to the person running the step: record it, do not pre-fill it. Issue #32 stays open until the [run record](#run-record) is filled in and committed.

The sections are independent after [M0](#m0-setup), with these prerequisites: M3 to M5 reuse the watch that M2 adds, and [M3.1](#m31-install-the-stand-in) (the stub `forgectl` and the `mode` file) is a prerequisite of M4, M5, M6.6 and M7. Sections M6 and M7 touch your **real crontab** and say so in their first line.

The acceptance bullet "every `--json` output validated against `schemas/`" has no step of its own: the golden-file tests do that on every platform, and [M1.6](#m16-make-verify-from-a-checkout-optional) runs them on a Mac. The steps here only check that `--json` output is JSON.

## How to run it

- Use a Mac (Apple silicon or Intel) with the stock `/bin/zsh`, `uv`, `git`, `python3` and `sqlite3` (the last two ship with the Xcode command line tools or macOS), and a forgectl that can read one pull request you can see (set up its host and token first; see [README, Dependencies](../README.md#dependencies)).
- Every command below runs in one interactive shell (`zsh` or `bash`; the snippets avoid shell-specific syntax). Keep that shell open: it holds the variables. Use an interactive shell so background jobs keep job control (a non-interactive shell starts `&` jobs with SIGINT ignored, which would make M3.5 meaningless). In zsh, `#` comments are only comments after `setopt interactivecomments` (M0.2 sets it).
- A backgrounded poll is always started as `"$PRW/pw" ... & POLLPID=$!`, never as the shell function `pw`: a function run with `&` is a forked subshell, so `$!` would not be the pr-watch process (M0.2).
- Result values for the record: `PASS` (expected result seen), `FAIL` (differs: copy the output), `OBS` (a **to be observed** item: copy the value and say whether the invariant holds), `SKIP` (with the reason).
- A `FAIL` or a broken invariant is a finding. Open an issue; do not edit the expected result to match.
- Signals are only ever sent to PIDs captured with `$!` or read from a file this checklist wrote, and only after `ps` shows the expected command: `prw_sig` (M0.2) checks that `$POLLPID` is alive and is a `pr-watch` before every `kill`. No step uses `pkill`, `killall` or a pattern.

## M0. Setup

What is isolated, and what is not. pr-watch's **state** is isolated with `XDG_STATE_HOME` and its **config** with `--config`, and the tools are installed into a scratch directory with `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR`. Your `HOME` and `XDG_CONFIG_HOME` are left alone on purpose: forgectl reads its configuration and credentials from them, and a scratch `HOME` would break every step that talks to a host. The only real-machine writes are the uv cache, the forgectl requests (read-only), and the two crontab sections M6 and M7.

### M0.1 Machine facts

```sh
uname -sm
sw_vers
python3 --version
uv --version
git --version
sqlite3 --version
id -un
echo "$SHELL"
```

Expected: `Darwin arm64` or `Darwin x86_64`, and every tool prints a version. Record them in the run record (macOS version, architecture, the shell).

- [ ] pass  - [ ] fail

### M0.2 Scratch directory and helpers

```sh
[ -n "$ZSH_VERSION" ] && setopt interactivecomments
PRW=$(mktemp -d /tmp/prw.XXXXXX)
case $PRW in /tmp/prw.??????) [ -d "$PRW" ] || PRW= ;; *) PRW= ;; esac
if [ -n "$PRW" ]; then
  export PRW
  chmod 700 "$PRW"
  mkdir -p "$PRW/state" "$PRW/out" "$PRW/stub" "$PRW/tools" "$PRW/bin"
  export XDG_STATE_HOME="$PRW/state"
  export UV_TOOL_DIR="$PRW/tools" UV_TOOL_BIN_DIR="$PRW/bin"
  export PR='github.com/OWNER/REPO#123'   # replace: one open PR you can read; it is only read
  echo "scratch: $PRW"
else
  echo "STOP: mktemp failed; run nothing below"
fi
```

Expected: `scratch: /tmp/prw.` and six characters (`/tmp` is a symlink to `/private/tmp` on macOS; pr-watch accepts a symlinked parent of the state directory). Anything else prints `STOP`: do not go on, since the variables are unset. Edit `PR` before going on.

Define the helpers once (they only touch `$PRW`, and a process only when `ps` shows `sleep 600` or, for `prw_sig`, a `pr-watch`). `$PRW/pw` is a script that `exec`s pr-watch, so a backgrounded `"$PRW/pw" ... &` has the pr-watch process itself as `$!` (the `exec` keeps the pid). The function `pw` is for foreground use only. A second `--config` on the command line replaces the first, so `pw --config OTHER ...` works.

```sh
cat > "$PRW/pw" <<EOF
#!/bin/sh
exec '$PRW/bin/pr-watch' --config '$PRW/config-base.yaml' "\$@"
EOF
chmod 755 "$PRW/pw"
pw() { "$PRW/pw" "$@"; }
prw_sig() { if [ -n "$POLLPID" ] && kill -0 "$POLLPID" 2>/dev/null && ps -o command= -p "$POLLPID" | grep -q pr-watch; then kill -"$1" "$POLLPID"; else echo "prw_sig: pid '$POLLPID' is not a live pr-watch; no signal sent"; return 1; fi; }
prw_wait_file() { i=0; while [ ! -s "$1" ] && [ "$i" -lt 100 ]; do sleep 0.2; i=$((i+1)); done; [ -s "$1" ]; }
prw_reset() { [ -n "$PRW" ] && rm -f "$PRW"/leader.pids "$PRW"/child.pids "$PRW"/leader.ps; }
prw_alive() { for p in $(cat "$PRW/child.pids" "$PRW/leader.pids" 2>/dev/null); do kill -0 "$p" 2>/dev/null && echo "ALIVE $(ps -o pid=,pgid=,command= -p "$p")"; done; echo "alive check done"; }
prw_reap() { for p in $(cat "$PRW/child.pids" "$PRW/leader.pids" 2>/dev/null); do if [ "$(ps -o command= -p "$p" 2>/dev/null)" = "sleep 600" ]; then kill -KILL "$p"; echo "reaped $p"; fi; done; }
```

- [ ] pass  - [ ] fail

### M0.3 Config files

```sh
cat > "$PRW/config-base.yaml" <<'EOF'
forgectl:
  timeout: 5s
EOF
chmod 600 "$PRW/config-base.yaml"
```

Expected: no output. (`forgectl.timeout: 5s` makes a stalled forgectl call end by itself after about 15 seconds, see M3 and M4.)

- [ ] pass  - [ ] fail

## M1. Install

### M1.1 Install pr-watch with the browse extra and forgectl

```sh
uv tool install --python 3.12 'pr-watch[browse] @ git+https://github.com/agent-workflow-kit/pr-watch'
uv tool install --python 3.12 git+https://github.com/agent-workflow-kit/forgectl
ls -l "$PRW/bin"
"$PRW/bin/pr-watch" --version
"$PRW/bin/forgectl" --version
"$PRW/tools/pr-watch/bin/python" -c 'import platform, sys, textual; print(sys.version.split()[0], platform.machine(), textual.__version__)'
```

To test an unreleased checkout instead, replace the first source with `"pr-watch[browse] @ file://$PWD"` run from the checkout. Expected: `pr-watch` and `forgectl` appear in `$PRW/bin`, both print a version (`pr-watch X.Y.Z`, `forgectl 0.55` or later), and the last command prints a Python 3.12 or newer, the machine's architecture (`arm64` on Apple silicon: a Rosetta `x86_64` here is a finding) and Textual 8.x. Record the pr-watch, forgectl and Python versions.

- [ ] pass  - [ ] fail

### M1.2 The core install works without the extra

```sh
UV_TOOL_DIR="$PRW/tools-core" UV_TOOL_BIN_DIR="$PRW/bin-core" uv tool install --python 3.12 'pr-watch @ git+https://github.com/agent-workflow-kit/pr-watch'
"$PRW/bin-core/pr-watch" browse; echo "exit=$?"
```

Expected: exit 2, an error naming the missing extra with the hint `uv tool install 'pr-watch[browse] @ git+https://github.com/agent-workflow-kit/pr-watch'` in the message and in `details.install` ([TUI](interface.md#tui): `details.kind: tui_missing`). Every other command works with this install (M1.3 can be run with it too).

- [ ] pass  - [ ] fail

### M1.3 `validate`, offline

```sh
export PATH="$PRW/bin:$PATH"
pw validate; echo "exit=$?"
pw validate --json; echo "exit=$?"
ls -A "$XDG_STATE_HOME"
```

Expected: exit 0, one `<name>: ok: <message>` line per check (`config`, `forgectl`, `state_dir`), the JSON object with `ok: true`, `forgectl.path` equal to `$PRW/bin/forgectl` and its version. `ls -A` prints nothing: `validate` creates no state directory ([`validate`](interface.md#validate)).

- [ ] pass  - [ ] fail

### M1.4 `validate` finds forgectl only on an absolute `PATH`

```sh
env PATH=/usr/bin:/bin "$PRW/bin/pr-watch" --config "$PRW/config-base.yaml" validate; echo "exit=$?"
env PATH="relative/dir:$PRW/bin" "$PRW/bin/pr-watch" --config "$PRW/config-base.yaml" validate; echo "exit=$?"
```

Expected: the first is exit 12 `tool_missing` (forgectl is not on `/usr/bin:/bin`: this is also what cron's default `PATH` would give). The second finds forgectl (the relative entry is ignored) and exits 0. pr-watch itself starts because the tool script's shebang is absolute.

- [ ] pass  - [ ] fail

### M1.5 `validate --probe` (one read-only host call)

```sh
pw validate --probe; echo "exit=$?"
```

Expected: exit 0 with a `probe` line naming the number of hosts; exit 20 `refused` or 12 `host_failed` means forgectl has no usable credential (fix that, then repeat). **SKIP** if you do not want a host call.

- [ ] pass  - [ ] fail  - [ ] skip

### M1.6 `make verify` from a checkout (optional)

```sh
cd /path/to/pr-watch-checkout && uv sync --all-extras && make verify
```

Expected: ruff, format, pyright, pytest and markdown lint pass. Record the passed and skipped counts. Tests that need `/proc` or `os.waitid` skip or take their macOS branch; a test that **fails** only on macOS is a finding. Return to the shell's earlier directory afterwards.

- [ ] pass  - [ ] fail  - [ ] skip

## M2. Watch, state directory modes and refusals

### M2.1 Add a watch and read the state tree

```sh
pw watch add "$PR" --owner checklist --json
stat -f '%Sp %Lp %Su %N' "$XDG_STATE_HOME/pr-watch" "$XDG_STATE_HOME/pr-watch"/* 
```

Expected: the watch is added and baselined. `stat` shows, for the `pr-watch` directory and `hooks-cwd`, `drwx------ 700` and your user; for `state.db` and any `-wal` or `-shm`, `-rw------- 600` and your user. `log.jsonl` and `cron.lock` do not exist yet: `watch add` writes no log record (the first poll creates `log.jsonl`, see M2.3) and M6 creates `cron.lock`. Record the listing ([Files](interface.md#files)).

- [ ] pass  - [ ] fail

### M2.2 A read-only command creates nothing

```sh
PRW_EMPTY=$(mktemp -d "$PRW/empty.XXXXXX")
XDG_STATE_HOME="$PRW_EMPTY" pw list --json; echo "exit=$?"
ls -A "$PRW_EMPTY"
```

Expected: an empty list, exit 0, and `ls -A` prints nothing (no directory, no `hooks-cwd`).

- [ ] pass  - [ ] fail

### M2.3 Unsafe modes are refused with exit 12 `state_unsafe`

Each line corrects its change before the next one. They touch only `$PRW/state`. The first poll (one read-only forgectl call) creates `log.jsonl`.

```sh
S="$XDG_STATE_HOME/pr-watch"
pw poll --pr "$PR" --json >/dev/null; echo "first poll exit=$?"; ls -l "$S/log.jsonl"
chmod 770 "$S";            pw list --json; echo "dir g+w exit=$?";        chmod 700 "$S"
chmod 707 "$S/hooks-cwd";  pw list --json; echo "hooks-cwd o+rwx exit=$?"; chmod 700 "$S/hooks-cwd"
chmod 664 "$S/state.db";   pw list --json; echo "state.db g+w exit=$?";   chmod 600 "$S/state.db"
chmod 666 "$S/log.jsonl";  pw poll --pr "$PR" --json >/dev/null; echo "log g+w exit=$?"; chmod 600 "$S/log.jsonl"
pw list --json >/dev/null; echo "restored exit=$?"
```

Expected: the first poll exits 0 and `log.jsonl` is `-rw-------`; the first three refusals print an error envelope with `code: state_unsafe` and exit 12; the log line exits 0 (a log that cannot be written never fails a command) and prints a `log.jsonl not written` notice on stderr (**to be observed**: the exact wording); the last line prints exit 0. Group- and world-readable but not writable (`chmod 750`) is also refused if the owner rule says so: **to be observed**, record the result of `chmod 750 "$S"; pw list; chmod 700 "$S"` ([Files](interface.md#files): "group- or world-writable").

- [ ] pass  - [ ] fail

### M2.4 A symlink is refused

```sh
mv "$XDG_STATE_HOME/pr-watch" "$PRW/state-real"
ln -s "$PRW/state-real" "$XDG_STATE_HOME/pr-watch"
pw list --json; echo "final component symlink exit=$?"
rm "$XDG_STATE_HOME/pr-watch" && mv "$PRW/state-real" "$XDG_STATE_HOME/pr-watch"
# a symlinked parent is allowed (the final component is the one checked)
ln -s "$PRW/state" "$PRW/state-link"
XDG_STATE_HOME="$PRW/state-link" pw list --json; echo "parent symlink exit=$?"
rm "$PRW/state-link"
# hooks-cwd as a symlink
mv "$XDG_STATE_HOME/pr-watch/hooks-cwd" "$PRW/hooks-real"
ln -s "$PRW/hooks-real" "$XDG_STATE_HOME/pr-watch/hooks-cwd"
pw list --json; echo "hooks-cwd symlink exit=$?"
rm "$XDG_STATE_HOME/pr-watch/hooks-cwd" && mv "$PRW/hooks-real" "$XDG_STATE_HOME/pr-watch/hooks-cwd"
pw list --json >/dev/null; echo "restored exit=$?"
```

Expected: the first and the third exit 12 `state_unsafe`; the second exits 0; the last exits 0. `rm` is applied only to the symlinks this step created.

- [ ] pass  - [ ] fail

### M2.5 `HOME` and `XDG_STATE_HOME` fallbacks

```sh
PRW_BEFORE=$(ls -A "$HOME/.local/state" 2>/dev/null | grep -c '^pr-watch$'); echo "pr-watch dirs before: $PRW_BEFORE"
env -i PATH="$PRW/bin:/usr/bin:/bin" "$PRW/bin/pr-watch" --config "$PRW/config-base.yaml" validate --json | head -c 200; echo
env -i HOME=relative XDG_STATE_HOME=relative PATH="$PRW/bin:/usr/bin:/bin" "$PRW/bin/pr-watch" --config "$PRW/config-base.yaml" validate --json | head -c 200; echo
echo "pr-watch dirs after: $(ls -A "$HOME/.local/state" 2>/dev/null | grep -c '^pr-watch$')"
```

Expected: both validate runs succeed (a relative `HOME` and a relative `XDG_STATE_HOME` are ignored: the account's home directory from the password database is used). The count after equals `PRW_BEFORE` (these runs create nothing: `validate` writes nothing). **To be observed**: that `pwd.getpwuid` agrees with `dscl . -read /Users/$(id -un) NFSHomeDirectory`.

- [ ] pass  - [ ] fail

## M3. Process groups and signals

pr-watch runs every forgectl call and every hook in its own session and process group, and on shutdown sends SIGTERM to those groups, then SIGKILL after 5 seconds (`SHUTDOWN_KILL_AFTER`), only while the group leader is unreaped ([Signals](interface.md#signals), [gateway.py](../src/pr_watch/gateway.py)). A stand-in `forgectl` makes a call that takes long enough to signal. It passes everything except `snapshot` to the real forgectl. A stalled call ends by itself after `forgectl.timeout` plus the 10 s slack (15 s with the base config), so paste each step's commands without a pause, and M3.2 and M3.3 together.

### M3.1 Install the stand-in

```sh
REAL_FORGECTL="$PRW/bin/forgectl"
cat > "$PRW/stub/forgectl" <<EOF
#!/bin/sh
# checklist stand-in: behaves as the real forgectl except for 'snapshot'
PRW='$PRW'
if [ "\$1" != snapshot ]; then exec '$REAL_FORGECTL' "\$@"; fi
mode=\$(cat "\$PRW/mode" 2>/dev/null || echo pass)
ps -o pid=,pgid=,ppid=,tty= -p \$\$ >> "\$PRW/leader.ps"
echo \$\$ >> "\$PRW/leader.pids"
case "\$mode" in
  pass)   exec '$REAL_FORGECTL' "\$@" ;;
  fail)   echo 'not json'; exit 0 ;;
  stall)  sleep 600 & echo \$! >> "\$PRW/child.pids"; wait ;;
  ignore) trap '' TERM; sleep 600 & echo \$! >> "\$PRW/child.pids"; wait ;;
  leak)   ( trap '' TERM; exec sleep 600 ) & echo \$! >> "\$PRW/child.pids"; trap 'exit 0' TERM; wait ;;
esac
EOF
chmod 755 "$PRW/stub/forgectl"
echo pass > "$PRW/mode"
export PATH="$PRW/stub:$PRW/bin:$(echo "$PATH" | sed "s#$PRW/bin:##")"
command -v forgectl
forgectl --version
```

Expected: `command -v forgectl` prints `$PRW/stub/forgectl`, and `--version` prints the real forgectl's version line. Modes: `pass` (real), `fail` (garbage output, a retryable per-PR failure), `stall` (the leader is `sh`, one `sleep 600` grandchild), `ignore` (the leader and its child ignore SIGTERM), `leak` (the leader exits on SIGTERM and leaves a grandchild that ignores SIGTERM). Set the mode with `echo MODE > "$PRW/mode"`.

- [ ] pass  - [ ] fail

### M3.2 The forgectl child is its own group leader

```sh
prw_reset; echo stall > "$PRW/mode"
"$PRW/pw" poll --pr "$PR" --json > "$PRW/out/m32.json" 2> "$PRW/out/m32.err" & POLLPID=$!
prw_wait_file "$PRW/leader.ps" && sleep 1
cat "$PRW/leader.ps"
echo "poll pid: $POLLPID, poll pgid: $(ps -o pgid= -p "$POLLPID")"
ps -o pid=,pgid=,tty=,command= -p "$(cat "$PRW/child.pids")"
```

Expected: the leader line has `pid == pgid` (it leads its own group), `ppid` equal to the pid of the poll (`$POLLPID`), the tty column `??` (no controlling terminal: a session of its own), and the poll's own pgid differs from the leader's. The `sleep 600` has the leader's pgid. Leave the poll running and go straight to M3.3 (it ends by itself after about 15 seconds).

- [ ] pass  - [ ] fail

### M3.3 SIGTERM during a forgectl call

```sh
prw_sig TERM; wait "$POLLPID"; echo "exit=$?"
prw_alive
cat "$PRW/out/m32.json" "$PRW/out/m32.err"
grep poll_end "$XDG_STATE_HOME/pr-watch/log.jsonl" | tail -1
sqlite3 -readonly "$XDG_STATE_HOME/pr-watch/state.db" 'select count(*) from leases'
```

Expected: `exit=143`, `prw_alive` prints no `ALIVE` line (the group got SIGTERM, so both the leader and the `sleep` are gone), no report on stdout, the last `poll_end` record has `interrupted` true, and the lease count is 0 (released). Reap anything left with `prw_reap` and record it.

- [ ] pass  - [ ] fail

### M3.4 SIGHUP during a forgectl call

```sh
prw_reset
"$PRW/pw" poll --pr "$PR" --json > "$PRW/out/m34.json" 2>&1 & POLLPID=$!
prw_wait_file "$PRW/child.pids" && sleep 1
prw_sig HUP; wait "$POLLPID"; echo "exit=$?"
prw_alive
```

Expected: `exit=129` and no `ALIVE` line.

- [ ] pass  - [ ] fail

### M3.5 SIGINT during a forgectl call

```sh
prw_reset
"$PRW/pw" poll --pr "$PR" --json > "$PRW/out/m35.json" 2>&1 & POLLPID=$!
prw_wait_file "$PRW/child.pids" && sleep 1
prw_sig INT; wait "$POLLPID"; echo "exit=$?"
prw_alive
```

Expected: `exit=130` and no `ALIVE` line. If the poll ignored the signal (the job started with SIGINT ignored), the shell is not interactive: stop the stalled poll with `prw_sig TERM`, run `set -m`, and repeat. Also press Ctrl-C once in a terminal on a poll run in the foreground (`pw poll --pr "$PR"`, in `stall` mode, in a second terminal tab) and confirm exit 130 and no `ALIVE` lines.

- [ ] pass  - [ ] fail

### M3.6 A forgectl that ignores SIGTERM is group-SIGKILLed after 5 seconds

```sh
prw_reset; echo ignore > "$PRW/mode"
"$PRW/pw" poll --pr "$PR" --json > "$PRW/out/m36.json" 2>&1 & POLLPID=$!
prw_wait_file "$PRW/child.pids" && sleep 1
START=$(date +%s); prw_sig TERM; wait "$POLLPID"; echo "exit=$? seconds=$(( $(date +%s) - START ))"
prw_alive
```

Expected: `exit=143`, `seconds` about 5 (between 4 and 8), no `ALIVE` line. The leader was still unreaped when the 5 seconds ran out, so the SIGKILL reached the whole group, `sleep` included.

- [ ] pass  - [ ] fail

### M3.7 The call deadline

```sh
prw_reset; echo stall > "$PRW/mode"
START=$(date +%s)
pw poll --pr "$PR" --json > "$PRW/out/m37.json" 2>&1; echo "exit=$? seconds=$(( $(date +%s) - START ))"
prw_alive
cat "$PRW/out/m37.json"
```

Expected: after about 15 seconds (`forgectl.timeout` 5 s plus the 10 s slack) the call ends as the local `killed` failure: the report's `failed` names the PR, exit 0, and no `ALIVE` line (the leader was alive at the deadline, so the group got SIGTERM and died).

- [ ] pass  - [ ] fail

### M3.8 Descendant that outlives a SIGTERMed leader (macOS has no `os.waitid`)

On Linux the gateway sees the leader exit without reaping it and SIGKILLs the rest of the group at once; without `os.waitid` (macOS) the leader can only be observed by reaping it, so the descendant is not killed ([gateway.py docstring](../src/pr_watch/gateway.py), comment on #46).

```sh
prw_reset; echo leak > "$PRW/mode"
"$PRW/pw" poll --pr "$PR" --json > "$PRW/out/m38.json" 2>&1 & POLLPID=$!
prw_wait_file "$PRW/child.pids" && sleep 1
prw_sig TERM; wait "$POLLPID"; echo "exit=$?"
sleep 2
prw_alive
```

Expected on macOS (**to be observed**): `exit=143` and **one `ALIVE` line**: the `sleep 600` grandchild survived. Record whether it did and its pgid. Then `prw_reap` and `prw_alive` (nothing alive).

Decision to record (issue #32, comment on #46): accept this residual risk, or add a fallback. The documented rule is that a forgectl call must not leave background work, and real forgectl does not (it ends its own children, `KILL_GRACE` 2 s). A fallback would have to signal the group id after reaping the leader, which risks hitting a recycled group id; kqueue `EVFILT_PROC` with `NOTE_EXIT` on the leader's pid would observe the exit without reaping. Write the decision in the run record.

- [ ] pass (matches the expectation)  - [ ] fail  - decision: accept / fallback needed

### M3.9 Concurrency: SIGTERM with several calls in flight

```sh
prw_reset; echo stall > "$PRW/mode"
"$PRW/pw" poll --full --json > "$PRW/out/m39.json" 2>&1 & POLLPID=$!
prw_wait_file "$PRW/child.pids" && sleep 2
prw_sig TERM; wait "$POLLPID"; echo "exit=$?"
prw_alive
```

Expected: `exit=143`, no `ALIVE` line. (With one watched PR there is one call; add more watches with `pw watch add ... --owner checklist` first if you want several in flight.)

- [ ] pass  - [ ] fail

Set the mode back: `echo pass > "$PRW/mode"`.

## M4. Leases without `/proc`

On Linux a lease holder is identified by pid, process start time (`/proc/<pid>/stat`) and boot id (`/proc/sys/kernel/random/boot_id`). On macOS there is no `/proc`: start time and boot id are recorded as `unknown`, and an unknown holder counts as alive until its lease expires ([Locks and commit](interface.md#locks-and-commit), [leases.py](../src/pr_watch/leases.py)). The expiry is `3 x forgectl.timeout`, never less than two forgectl calls plus 30 seconds (each call has the 10 s slack): with `forgectl.timeout: 5s` it is `max(15, 2 x 15 + 30) = 60` seconds. A stalled poll ends by itself (and releases its lease) after `forgectl.timeout` plus the slack, 15 s with the base config, which is too short for M4.1 to M4.3. These steps therefore use `forgectl.timeout: 60s` (the stalled poll lives 70 s; the lease expires `max(180, 2 x 70 + 30) = 180` seconds after it was taken). Paste M4.1 to M4.3 without a pause.

### M4.1 What the lease row holds

```sh
prw_reset; echo stall > "$PRW/mode"
printf 'forgectl:\n  timeout: 60s\n' > "$PRW/config-m4.yaml"; chmod 600 "$PRW/config-m4.yaml"
"$PRW/pw" --config "$PRW/config-m4.yaml" poll --pr "$PR" --json > "$PRW/out/m41.json" 2>&1 & POLLPID=$!
prw_wait_file "$PRW/child.pids" && sleep 1
sqlite3 -readonly -header "$XDG_STATE_HOME/pr-watch/state.db" 'select pr_id, holder_pid, holder_start, boot_id, expires_at from leases'
echo "poll pid: $POLLPID"; date -u +%Y-%m-%dT%H:%M:%SZ
```

Expected: one row, `holder_pid` equal to `$POLLPID`, `holder_start` and `boot_id` both `unknown`, and `expires_at` about 180 seconds ahead (the time printed by `date`, plus three minutes).

- [ ] pass  - [ ] fail

### M4.2 A second poll sees a live lease

```sh
pw poll --pr "$PR" --json; echo "exit=$?"
```

Expected: exit 0, `already_running: true` (a `--pr` poll reports `polled: false`) and `prs.skipped_locked` 1.

- [ ] pass  - [ ] fail

### M4.3 Crash: the dead holder is still treated as alive until expiry

```sh
prw_sig KILL; wait "$POLLPID" 2>/dev/null; echo "poll killed"
ps -p "$POLLPID" >/dev/null 2>&1 && echo "STILL RUNNING" || echo "poll is gone"
prw_alive
sqlite3 -readonly "$XDG_STATE_HOME/pr-watch/state.db" 'select holder_pid, holder_start, expires_at from leases'
pw poll --pr "$PR" --json; echo "exit=$?"
```

Expected: the poll is gone, the stalled forgectl group is still alive (SIGKILL of the poll cannot clean it up: `prw_alive` shows the leader and the `sleep`; run `prw_reap` and confirm nothing is left), the lease row remains with `unknown` identity, and the immediate poll reports `already_running: true` even though the holder is dead. This is the conservative rule, not a bug.

- [ ] pass  - [ ] fail

### M4.4 Takeover after expiry

```sh
prw_reap; prw_alive
echo pass > "$PRW/mode"
sqlite3 -readonly "$XDG_STATE_HOME/pr-watch/state.db" 'select expires_at from leases'; date -u +%Y-%m-%dT%H:%M:%SZ
```

Wait until the UTC time printed by `date` is past `expires_at` (about three minutes after M4.1), then:

```sh
pw prune --dry-run --json
pw poll --pr "$PR" --json; echo "exit=$?"
sqlite3 -readonly "$XDG_STATE_HOME/pr-watch/state.db" 'select count(*) from leases'
```

Expected: `prune --dry-run` reports `leases_deleted` 1 (an expired lease is deleted by prune); the poll takes the lease over without `already_running` (`prs.snapshotted` 1, or `unchanged` 1 if nothing changed), exit 0, and the lease count afterwards is 0.

- [ ] pass  - [ ] fail

### M4.5 Boot-id substitute: what macOS offers

Record these values (the first three are read-only):

```sh
sysctl -n kern.boottime
sysctl -n kern.bootsessionuuid
sysctl -n kern.bootsessionuuid
LC_ALL=C TZ=UTC ps -o lstart= -p $$
```

Expected: `kern.boottime` prints `{ sec = ..., usec = ... } <date>`; `kern.bootsessionuuid` prints a UUID, the same on both calls (**to be observed**: that it is stable within a boot and differs after M4.6); `ps -o lstart=` prints five words (`Mon Nov  2 14:40:01 2026`, with a padded day). These are the candidates for the "read the boot time through the gateway so a reboot is detected earlier" decision (comment on #12): `sysctl` and `ps` would have to be run through the gateway, which makes no direct subprocess call. Record the decision: keep `unknown` (a reboot is noticed only when the lease expires, at most one hour; every commit is a compare-and-set, so a stale holder cannot corrupt state) or implement the substitute (new issue).

- [ ] pass  - [ ] fail  - decision: keep / implement

### M4.6 Lease across a reboot (optional, one reboot)

This step shows that a reboot does not release a lease early on macOS. It needs a long lease and a reboot, so run it last.

```sh
prw_reset; echo stall > "$PRW/mode"
cat > "$PRW/config-lease.yaml" <<'EOF'
forgectl:
  timeout: 600s
EOF
"$PRW/pw" --config "$PRW/config-lease.yaml" poll --pr "$PR" --json > "$PRW/out/m46.json" 2>&1 & POLLPID=$!
prw_wait_file "$PRW/child.pids" && sleep 1
sqlite3 -readonly "$XDG_STATE_HOME/pr-watch/state.db" 'select holder_start, boot_id, expires_at from leases'
prw_sig KILL; prw_reap
sysctl -n kern.bootsessionuuid
echo "$PRW"
```

The expiry is 30 minutes ahead (`3 x 600 s`). Reboot the Mac now. After login, in a new shell:

```sh
export PRW=/tmp/prw.XXXXXX      # the value printed above
export XDG_STATE_HOME="$PRW/state" PR='github.com/OWNER/REPO#123'
export PATH="$PRW/stub:$PRW/bin:$PATH"
pw() { "$PRW/pw" "$@"; }
echo pass > "$PRW/mode"
sysctl -n kern.bootsessionuuid
sqlite3 -readonly "$XDG_STATE_HOME/pr-watch/state.db" 'select holder_start, boot_id, expires_at from leases'
pw poll --pr "$PR" --json
```

Expected (**to be observed**): `kern.bootsessionuuid` differs from before; the lease row survived the reboot with `unknown` identity; the poll reports `already_running: true` although the holder cannot exist after a reboot (on Linux the boot id would have changed and the lease would be taken over at once). After the printed `expires_at` has passed, the same poll takes it over. Record the time between the reboot and the takeover, and the decision of M4.5.

- [ ] pass  - [ ] fail  - [ ] skip

Set the mode back: `echo pass > "$PRW/mode"`.

## M5. Hooks

Hooks run as `/bin/sh -c <cmd>`, in `<state>/hooks-cwd`, in their own session, with stdin a pipe carrying the payload, stdout and stderr to `/dev/null`, a cleaned environment, and the group SIGTERMed at the timeout, then SIGKILLed 5 seconds later, only while the leader is unreaped ([Hooks](interface.md#hooks), comment on #28). The `fail` mode makes the stand-in forgectl return garbage, and `failures.unreachable_after: 1` turns one failure into an `unreachable` event and a `post-poll`, which gives two hook runs without changing the PR.

A PR that is already `unreachable` and fails again fires no `on-unreachable` and no `post-poll` ([hooks.py](../src/pr_watch/hooks.py), `should_post_poll`), and re-adding a watch does not reset that. So every step that needs hooks first removes the watch, re-adds it in `pass` mode (baselined, active) and only then switches to `fail`, and every config below sets `unreachable_after: 1` so that the one failing run makes the PR newly unreachable. `prw_fresh` does this; each failing run in M5 uses a fresh watch.

```sh
prw_fresh() {
  echo pass > "$PRW/mode"
  pw watch remove "$PR" --owner checklist >/dev/null
  pw watch add "$PR" --owner checklist >/dev/null
  echo fail > "$PRW/mode"
  pw list --json --pr "$PR" | head -c 300; echo
}
```

Expected after each `prw_fresh`: the listed PR is `active` (record it if it is not; the step after it will not fire hooks).

### M5.1 Environment, stdio and group

```sh
prw_reset
cat > "$PRW/hook-env.sh" <<EOF
#!/bin/sh
d='$PRW/out'
n="\$PR_WATCH_HOOK"
env | sort > "\$d/\$n.env"
cat > "\$d/\$n.stdin"
pwd -P > "\$d/\$n.pwd"
ps -o pid=,ppid=,pgid=,tty= -p \$\$ > "\$d/\$n.ps"
lsof -a -p \$\$ -d 0,1,2 > "\$d/\$n.lsof" 2>&1
echo "to-stdout"; echo "to-stderr" >&2
EOF
chmod 700 "$PRW/hook-env.sh"
cat > "$PRW/config-hooks.yaml" <<EOF
forgectl:
  timeout: 5s
failures:
  unreachable_after: 1
hooks:
  on-unreachable: [{cmd: "$PRW/hook-env.sh", timeout: 30, async: false}]
  post-poll: [{cmd: "$PRW/hook-env.sh", timeout: 30, async: false}]
EOF
chmod 600 "$PRW/config-hooks.yaml"
prw_fresh
GITHUB_TOKEN=prw-canary-token GH_TOKEN=prw-canary-token GITLAB_TOKEN=prw-canary-token FORGECTL_CANARY=prw-canary PR_WATCH_CANARY=1 \
  pw --config "$PRW/config-hooks.yaml" poll --pr "$PR" --json; echo "exit=$?"
ls "$PRW/out"
```

Expected: exit 0, the report shows `hooks.run` 2 and `events.unreachable` 1, and `$PRW/out` holds `on-unreachable.*` and `post-poll.*` files. Nothing of `to-stdout` or `to-stderr` appears in the terminal (stdout and stderr go to `/dev/null`). The canary tokens are fake, and the non-snapshot forgectl calls (`whoami`, `--version`) go through the stand-in to the **real** forgectl with them in its environment: if that gives an auth error and the report shows a host failure instead of `events.unreachable` 1, record it and repeat the step with only `PR_WATCH_CANARY=1 FORGECTL_CANARY=prw-canary PRW_CANARY_TOKEN=prw-canary-token` (the checks in M5.2 look for the string `canary`).

- [ ] pass  - [ ] fail

### M5.2 What the hook saw

```sh
cat "$PRW/out/on-unreachable.pwd"; echo "$XDG_STATE_HOME/pr-watch/hooks-cwd (resolved: /private/tmp/...)"
cat "$PRW/out/on-unreachable.ps"
cat "$PRW/out/on-unreachable.lsof"
cut -d= -f1 "$PRW/out/on-unreachable.env" | tr '\n' ' '; echo
grep -c canary "$PRW/out"/*.env
grep -E '^(HOME|PATH|PR_WATCH_[A-Z_]+|XDG_[A-Z_]+)=' "$PRW/out/on-unreachable.env"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["hook"], d["object_type"], d["event_id"], d["type"])' "$PRW/out/on-unreachable.stdin"
```

Expected: the working directory resolves to `.../state/pr-watch/hooks-cwd` (macOS shows `/private/tmp/...` for a `/tmp` path); the `ps` line has `pgid` equal to the hook's `pid` or its `ppid` (the `sh -c` leader) and tty `??`; `lsof` shows fd 0 as a pipe, and fds 1 and 2 as `/dev/null` (`CHR` on `/dev/null`); the environment names are `PATH`, `HOME`, `LANG`, `TERM`, `XDG_*` as set, `PR_WATCH_HOOK`, `PR_WATCH_ID`, `PR_WATCH_EVENT_ID`, `PR_WATCH_OBJECT_TYPE`, plus whatever `/bin/sh` adds itself (**to be observed**: `PWD`, `SHLVL`, `_`, `OLDPWD`, and on macOS possibly `__CF_USER_TEXT_ENCODING`; record every other name as a finding); the canary count is 0 for every `.env` file (no token variable, no `FORGECTL_*`, and `PR_WATCH_CANARY` is a config input, never forwarded); `PR_WATCH_HOOK=on-unreachable`, `PR_WATCH_EVENT_ID` equal to the stdin's `event_id`; `HOME` and `XDG_STATE_HOME` absolute; and the stdin payload parses as JSON with the event's fields.

- [ ] pass  - [ ] fail

### M5.3 A relative `HOME` and `XDG_*` are replaced or dropped

```sh
rm -f "$PRW/out"/*.env
prw_fresh
HOME=relhome XDG_CONFIG_HOME=relcfg \
  pw --config "$PRW/config-hooks.yaml" poll --pr "$PR" --json >/dev/null; echo "exit=$?"
grep -E '^(HOME|XDG_[A-Z_]+)=' "$PRW/out/on-unreachable.env" "$PRW/out/post-poll.env"
dscl . -read "/Users/$(id -un)" NFSHomeDirectory
```

Expected: the fresh watch becomes `unreachable` in this run, so both `on-unreachable.env` and `post-poll.env` are written; `HOME=` in each equals the `NFSHomeDirectory` (the password-database home), and there is no `XDG_CONFIG_HOME=relcfg` line.

- [ ] pass  - [ ] fail

### M5.4 Timeout: SIGTERM, then SIGKILL after 5 seconds

```sh
prw_reset
cat > "$PRW/hook-hang.sh" <<EOF
#!/bin/sh
echo \$\$ >> '$PRW/leader.pids'
sleep 600 &
echo \$! >> '$PRW/child.pids'
wait
EOF
cat > "$PRW/hook-ignore.sh" <<EOF
#!/bin/sh
trap '' TERM
echo \$\$ >> '$PRW/leader.pids'
sleep 600 &
echo \$! >> '$PRW/child.pids'
wait
EOF
chmod 700 "$PRW/hook-hang.sh" "$PRW/hook-ignore.sh"
cat > "$PRW/config-hang.yaml" <<EOF
forgectl:
  timeout: 5s
failures:
  unreachable_after: 1
hooks:
  post-poll: [{cmd: "$PRW/hook-hang.sh", timeout: 2, async: false}]
EOF
chmod 600 "$PRW/config-hang.yaml"
prw_fresh
START=$(date +%s)
pw --config "$PRW/config-hang.yaml" poll --pr "$PR" --json >/dev/null; echo "exit=$? seconds=$(( $(date +%s) - START ))"
prw_alive
grep hook_run "$XDG_STATE_HOME/pr-watch/log.jsonl" | tail -1
```

Expected: the hook is stopped after about 2 seconds (the poll takes a few seconds in total), no `ALIVE` line, and the last `hook_run` record has `timed_out` true. Then repeat with the `ignore` variant: change the config's `cmd` to `$PRW/hook-ignore.sh`, run `prw_fresh` again (the PR is `unreachable` after the first run, and a second failing run would fire nothing), and run the poll again: expect about 7 seconds (2 s timeout plus 5 s), no `ALIVE` line.

- [ ] pass  - [ ] fail

### M5.5 A descendant outliving the hook's leader is not killed (documented)

```sh
prw_reset
cat > "$PRW/hook-leak.sh" <<EOF
#!/bin/sh
( trap '' TERM; exec sleep 600 ) &
echo \$! >> '$PRW/child.pids'
EOF
chmod 700 "$PRW/hook-leak.sh"
sed "s#hook-hang.sh#hook-leak.sh#" "$PRW/config-hang.yaml" > "$PRW/config-leak.yaml"
prw_fresh
pw --config "$PRW/config-leak.yaml" poll --pr "$PR" --json >/dev/null; echo "exit=$?"
sleep 1; prw_alive
```

Expected: the hook's leader exits at once and the `sleep 600` survives (**one `ALIVE` line**): "a hook must not leave background work" ([Hooks](interface.md#hooks), Residual risks); this is the same on Linux. Run `prw_reap`.

- [ ] pass  - [ ] fail

### M5.6 Signals while a hook runs

```sh
prw_reset
cat > "$PRW/config-hang-long.yaml" <<EOF
forgectl:
  timeout: 5s
failures:
  unreachable_after: 1
hooks:
  post-poll: [{cmd: "$PRW/hook-hang.sh", timeout: 600, async: false}]
EOF
chmod 600 "$PRW/config-hang-long.yaml"
BEFORE=$(grep -c hook_run "$XDG_STATE_HOME/pr-watch/log.jsonl"); echo "hook_run before: $BEFORE"
for SIG in TERM INT HUP; do
  prw_reset; prw_fresh
  "$PRW/pw" --config "$PRW/config-hang-long.yaml" poll --pr "$PR" --json >/dev/null 2>&1 & POLLPID=$!
  prw_wait_file "$PRW/child.pids" && sleep 1
  prw_sig "$SIG"; wait "$POLLPID"; echo "$SIG exit=$?"
  prw_alive
done
echo "hook_run after: $(grep -c hook_run "$XDG_STATE_HOME/pr-watch/log.jsonl")"
```

Expected: `TERM exit=143`, `INT exit=130`, `HUP exit=129`, and no `ALIVE` line after any of them (the hook's group got SIGTERM, then SIGKILL after 5 seconds if needed). A hook stopped by a shutdown signal writes no `hook_run` record (the count after equals `BEFORE`). Each pass of the loop uses a fresh watch (`prw_fresh`), so `post-poll` fires in each. Run `prw_reap` for any survivor.

- [ ] pass  - [ ] fail

Set the mode back: `echo pass > "$PRW/mode"`.

## M6. Cron (real crontab)

**This section reads and, from M6.5 on, writes your real crontab.** It takes a backup first, uses `--dry-run` before every write, and ends with `uninstall` and a comparison with the backup. Do not run it on a machine whose crontab holds anything you cannot restore from the backup. If anything goes wrong, the restore is `crontab "$PRW/crontab.backup"` when the backup is non-empty, and `crontab -r` only when the backup is empty (you had no crontab, so removing the table is the restore): `prw_restore` below does exactly that and nothing else. No other step types `crontab -r`; pr-watch itself calls it only when an uninstall leaves the crontab empty.

### M6.1 Before touching anything

```sh
rm -f "$PRW/backup.ok"
crontab -l > "$PRW/crontab.backup" 2> "$PRW/crontab.backup.err"; rc=$?; echo "crontab -l exit=$rc"
cat "$PRW/crontab.backup.err"; wc -c "$PRW/crontab.backup"
ls -l /var/mail/"$(id -un)" 2>&1 | head -1
if [ "$rc" -ne 0 ] && ! grep -q 'no crontab for' "$PRW/crontab.backup.err"; then
  echo "STOP: crontab -l failed for another reason (Full Disk Access?); skip M6 and M7"
elif grep -q 'pr-watch managed' "$PRW/crontab.backup"; then
  echo "STOP: the crontab already holds a pr-watch managed block; skip M6 and M7"
else
  : > "$PRW/backup.ok"; echo "backup ok"
fi
prw_restore() {
  [ -f "$PRW/backup.ok" ] || { echo "STOP: no verified backup"; return 1; }
  if [ -s "$PRW/crontab.backup" ]; then crontab "$PRW/crontab.backup"; else crontab -r; fi
}
```

Continue with M6 and M7 only if this printed `backup ok`.

Expected: **to be observed**: with no crontab, the exact wording (`crontab: no crontab for <user>`) and the exit status (1 on vixie 4.x); with one, exit 0 and the content in the backup. Record both. A failure with `Operation not permitted` or similar means the terminal lacks Full Disk Access (System Settings > Privacy & Security > Full Disk Access): grant it to the terminal, restart it, and repeat. pr-watch maps any crontab failure to exit 1 `internal` with a fixed hint and does not print crontab's own text, so the user would see no cause: record whether the hint should name this (comment on #29).

- [ ] pass  - [ ] fail  - decision (hint names Full Disk Access): yes / no

### M6.2 The sandbox config and env file

The cron run must not touch your real pr-watch state, and it must show what cron's environment looks like. The env file is sourced before `poll` (`. 'env_file' && pr-watch poll --quiet`), so it ends with `true`.

The cron entry has no `--config` ([interface.md, Cron](interface.md#cron), `build_block` in [cron.py](../src/pr_watch/cron.py)), so under cron pr-watch loads **your real** `~/.config/pr-watch` configuration (`config.yaml` and `config.d`) and would run its hooks against the scratch state. Check that there is none:

```sh
ls -A "$HOME/.config/pr-watch" 2>&1
```

Expected: `No such file or directory`, or nothing. If it lists anything, **STOP**: skip M6.5 to M7 (the dry runs M6.3 and M6.4 are still safe), since a hook of yours could fire from cron. The env file below exports `PR_WATCH_FORGECTL__TIMEOUT=5s`, so the 5 s timeout of `config-cron.yaml` (which only the install command reads) holds under cron too.

```sh
cat > "$PRW/cron-env.sh" <<EOF
export XDG_STATE_HOME='$PRW/state'
export PR_WATCH_FORGECTL__TIMEOUT=5s
{
  date
  echo "user=\$(id -un) shell=\$0 sh=\$(readlink /bin/sh 2>/dev/null || echo /bin/sh)"
  echo "pwd=\$(pwd)"
  env | sort
  echo "command -v pr-watch: \$(command -v pr-watch)"
  echo "command -v forgectl: \$(command -v forgectl)"
  '$PRW/bin/pr-watch' validate --json
  echo "validate exit=\$?"
} > '$PRW/cron-run.txt' 2>&1
true
EOF
chmod 600 "$PRW/cron-env.sh"
cat > "$PRW/config-cron.yaml" <<EOF
forgectl:
  timeout: 5s
cron:
  env_file: $PRW/cron-env.sh
EOF
chmod 600 "$PRW/config-cron.yaml"
env -i PATH=/usr/bin:/bin /bin/sh -c ". '$PRW/cron-env.sh' && echo sourced-ok"; echo "exit=$?"
rm -f "$PRW/cron-run.txt"
```

Expected: `sourced-ok`, exit 0 (`/bin/sh` on macOS is bash in POSIX mode; the `. 'file' && ...` form works). The `env -i` run is only a local rehearsal and writes `cron-run.txt`, which the `rm` removes again.

- [ ] pass  - [ ] fail

### M6.3 Install, dry run

```sh
pw --config "$PRW/config-cron.yaml" cron install --every 1m --dry-run --json; echo "exit=$?"
pw --config "$PRW/config-cron.yaml" cron install --every 1m --dry-run; echo "exit=$?"
pw cron uninstall --dry-run --json; echo "exit=$?"
ls -A "$XDG_STATE_HOME/pr-watch" | grep -c cron.lock
crontab -l | cmp - "$PRW/crontab.backup" 2>&1; echo "crontab unchanged exit=$?"
```

Expected: exit 0, `dry_run: true`, `block` an array of the lines a real run would write:

```text
# pr-watch managed v1 every=1m
PATH=<home>/.local/bin:<forgectl dir>:/usr/local/bin:/usr/bin:/bin
* * * * * . '<PRW>/cron-env.sh' && '<PRW>/bin/pr-watch' poll --quiet
# pr-watch managed end
```

(`<home>` is your absolute home directory, written out: the line has no `~`, and `<home>/.local/bin` is left out with a warning if it fails the same ownership check; the `PATH=` line holds `$PRW/stub` or `$PRW/bin` as the forgectl directory, depending on which `forgectl` the shell resolves; both are user-owned and not group-writable); `warnings` empty; uninstall's dry run reports nothing to remove; no `cron.lock` was created; and the crontab is byte for byte the backup (`cmp` silent; with no backup crontab `crontab -l` fails and the comparison is of two empty inputs: record).

- [ ] pass  - [ ] fail

### M6.4 forgectl resolution and the `PATH=` line (dry runs only)

```sh
# a: forgectl only in a group-writable directory (scratch, mimics an admin-group Homebrew directory)
mkdir "$PRW/gw" && chmod 775 "$PRW/gw" && ln -s "$PRW/bin/forgectl" "$PRW/gw/forgectl"
env PATH="$PRW/gw:/usr/bin:/bin" "$PRW/bin/pr-watch" --config "$PRW/config-cron.yaml" cron install --every 1m --dry-run --json
# b: forgectl not on PATH
env PATH="/usr/bin:/bin" "$PRW/bin/pr-watch" --config "$PRW/config-cron.yaml" cron install --every 1m --dry-run --json
# c: forgectl found only through a relative PATH entry
( cd "$PRW" && env PATH="bin:/usr/bin:/bin" "$PRW/bin/pr-watch" --config "$PRW/config-cron.yaml" cron install --every 1m --dry-run --json )
# d: the real Homebrew directories
stat -f '%Sp %Su:%Sg %N' /opt/homebrew/bin /usr/local/bin 2>&1
command -v forgectl
```

Expected: in (a) and (c) the directory is left out of `PATH=` and `warnings` says so; in (b) `warnings` says forgectl was not found and `PATH=` has no forgectl directory. Each is exit 0 (a warning, not a refusal). For (d) (comment on #29, **to be observed**): record whether `/opt/homebrew/bin` and `/usr/local/bin` are group-writable (`drwxrwxr-x ... admin`). If forgectl lives in one of them, `cron install` would leave it out of `PATH=`: record whether that makes `cron install` unusable on a stock Homebrew install and what the user has to do (for example install forgectl with `uv tool`, which puts it in `~/.local/bin`, owned by the user). Also note that the block writes `/usr/local/bin:/usr/bin:/bin` and not `/opt/homebrew/bin` unless it is forgectl's directory.

- [ ] pass  - [ ] fail

### M6.5 Install for real

Only continue after the dry runs above were clean, M6.1 printed `backup ok` and M6.2's configuration check was clean. Between the install and the uninstall, a `pr-watch poll --quiet` entry runs every minute against **scratch state** (the env file overrides `XDG_STATE_HOME`) with the stand-in `forgectl` in `pass` mode, so a cron poll leaves no `sleep 600` behind. The entry has no `--config`: it uses the default configuration (which M6.2 checked is empty) plus the `PR_WATCH_*` overrides of the env file, not `config-cron.yaml`.

```sh
echo pass > "$PRW/mode"
pw --config "$PRW/config-cron.yaml" cron install --every 1m --json; echo "exit=$?"
crontab -l
ls -l "$XDG_STATE_HOME/pr-watch/cron.lock"; stat -f '%Sp %Lp %Su' "$XDG_STATE_HOME/pr-watch/cron.lock"
```

Expected: exit 0, `installed: true`, `changed: true`, `replaced: false`; `crontab -l` shows your earlier lines unchanged and the block as printed by the dry run (**to be observed**: that `crontab -` with the table on stdin is accepted by macOS crontab, and whether it prompts or fails with a permission message); `cron.lock` is `-rw-------`, yours. Repeat the same install: `changed: false`, `replaced: true`.

- [ ] pass  - [ ] fail

### M6.6 The block under cron's real environment

Wait for the next two minute boundaries, then:

```sh
prw_wait_file "$PRW/cron-run.txt" || echo "cron has not run yet; wait and repeat (also check Full Disk Access for /usr/sbin/cron)"
cat "$PRW/cron-run.txt"
grep -E 'poll_(start|end)' "$XDG_STATE_HOME/pr-watch/log.jsonl" | tail -2
ls -l /var/mail/"$(id -un)" 2>&1 | head -1
```

Expected (**to be observed**: all of the environment values): `cron-run.txt` exists, proving the entry ran and the env file was sourced by cron's `/bin/sh` (its own `echo` lines appear); `PATH=` equals the block's line (cron's default `/usr/bin:/bin` is replaced; `<home>/.local/bin` is an absolute directory there); `command -v forgectl` prints the directory written in the block; `validate --json` inside cron reports `ok: true` and the forgectl path and version, with `validate exit=0`; `HOME`, `LOGNAME` and `SHELL=/bin/sh` are set, and the working directory is your home. One check specific to the `<home>/.local/bin` entry: whether `command -v pr-watch` finds a `pr-watch` (it is probably not found, because the scratch install is in `$PRW/bin`, which is not on the block's `PATH`, unless you have your own `pr-watch` in `~/.local/bin`; the entry itself uses an absolute path, so the poll works either way: record it); and `poll_start` and `poll_end` records in `log.jsonl` with a timestamp that matches the cron run. Nothing new should land in the mailbox (`--quiet`, and the env file sends its output to a file); record a changed size.

- [ ] pass  - [ ] fail

### M6.7 crontab limits (optional, still the real crontab)

a. A block line of exactly 999 bytes is accepted by vixie 4.x and 1000 bytes is refused by pr-watch before `crontab` sees it. The `env_file` path makes the entry line long. A file name is at most 255 bytes on APFS, so the path is built from `$1` directories of 200 bytes each plus a file name `e<pad>.sh` whose `pad` bytes tune the total length byte by byte:

```sh
PY="$PRW/tools/pr-watch/bin/python"
mklong() {   # $1 = number of 200-byte directories, $2 = pad bytes in the file name (0 to 250)
  rm -rf "$PRW/long"; d="$PRW/long"; mkdir -p "$d" && chmod 700 "$d"
  i=0; while [ "$i" -lt "$1" ]; do d="$d/$("$PY" -c "print('a'*200)")"; mkdir "$d" && chmod 700 "$d"; i=$((i+1)); done
  f="$d/e$("$PY" -c "print('a'*$2)").sh"
  cp "$PRW/cron-env.sh" "$f"; chmod 600 "$f"
  printf 'cron:\n  env_file: %s\nforgectl:\n  timeout: 5s\n' "$f" > "$PRW/config-long.yaml"; chmod 600 "$PRW/config-long.yaml"
}
mkline() {   # the longest block line, in bytes
  pw --config "$PRW/config-long.yaml" cron install --every 1m --dry-run --json | "$PY" -c 'import json,sys; print(max(len(l.encode()) for l in json.load(sys.stdin)["block"]))'
}
mklong 4 0; L0=$(mkline); echo "L0=$L0"
```

`L0` is the longest line with no pad (the entry line; about 900, below 999, so the dry run succeeds; if `mkline` fails with a traceback, use `mklong 3 0` and a pad of up to 250 for the rest). Then rebuild with the pad that makes the line exactly 999 bytes and check:

```sh
mklong 4 $((999 - L0)); mkline
```

Expected: `mkline` prints 999 (the length grows one byte per pad byte; if it does not, adjust the pad), and the block installs: `pw --config "$PRW/config-long.yaml" cron install --every 1m --json`, then `crontab -l | awk '{ print length($0) }' | sort -n | tail -1` prints 999 and `crontab -l` shows the block intact. Then `mklong 4 $((1000 - L0)); mkline` prints 1000 and `pw --config "$PRW/config-long.yaml" cron install --every 1m --dry-run --json` is refused with `config_invalid` and an unchanged crontab (`crontab -l | cmp - ...` against the 999 state, or against the backup if you skip the install). A pad over 250 bytes does not fit one name: use one more directory and a smaller pad. Finish with `rm -rf "$PRW/long"` and `pw --config "$PRW/config-cron.yaml" cron install --every 1m --json` to go back to the M6.5 block.

b. A table that does not end in a newline (vixie 4.x refuses it; pr-watch adds the newline). The test file is built from your **backup** with its trailing newline stripped, so no entry of yours is at risk, and the backup is restored on the next line. It needs a non-empty backup; with an empty one it is skipped:

```sh
if [ -f "$PRW/backup.ok" ] && [ -s "$PRW/crontab.backup" ]; then
  printf '%s' "$(cat "$PRW/crontab.backup")" > "$PRW/nonl"
  crontab "$PRW/nonl"; echo "crontab file exit=$?"
  crontab "$PRW/crontab.backup"; echo "restored from backup exit=$?"
  crontab -l | cmp - "$PRW/crontab.backup"; echo "cmp exit=$?"
else
  echo "SKIP: no non-empty verified backup"
fi
pw --config "$PRW/config-cron.yaml" cron install --every 1m --json >/dev/null; echo "M6.5 block back: exit=$?"
```

Expected (**to be observed**): vixie 4.x may refuse the table or accept it; record the first exit status. The restore exits 0 and `cmp` is silent (the crontab is the backup again; the managed block is put back by the last line for M6.8). Do not edit the commands: the file written is the backup minus its final newline, nothing else.

- [ ] pass  - [ ] fail  - [ ] skip

### M6.8 Uninstall

```sh
pw cron uninstall --dry-run --json; echo "exit=$?"
pw cron uninstall --json; echo "exit=$?"
crontab -l 2>&1; echo "crontab -l exit=$?"
crontab -l 2>/dev/null | cmp - "$PRW/crontab.backup"; echo "restored exactly: exit=$?"
```

If `cmp` reports a difference you cannot explain by the final newline, restore with `prw_restore` (that is `crontab "$PRW/crontab.backup"`, or `crontab -r` only when the backup is empty, the no-crontab original) and record what differed.

Expected: the dry run reports the block would be removed; the real run exits 0 with the block removed. If your original crontab had content, `crontab -l` equals the backup byte for byte (`cmp` silent), except that a missing final newline in the original is now present. If it was empty, pr-watch calls `crontab -r` itself because nothing is left: **to be observed**: its exit status and that `crontab -l` now says there is no crontab (the same wording as M6.1). A second `cron uninstall` is a no-op (exit 0, nothing removed). Finally confirm no `pr-watch` line remains: `crontab -l 2>/dev/null | grep -c pr-watch` prints 0. Leave no entry behind: if this step failed, fix it before going on.

- [ ] pass  - [ ] fail

## M7. A real poll under cron (optional, real crontab)

M6.6 proved the environment. This step proves a poll that calls forgectl through the block's `PATH`. It uses the same scratch state and the PR watched in M2.1, and the real forgectl through the stand-in (mode `pass`), with the same backup and uninstall discipline as M6. **It writes your real crontab again.**

Only after M6 passed its uninstall step (the crontab is the backup again), M6.1 printed `backup ok` and M6.2's configuration check was clean:

```sh
echo pass > "$PRW/mode"; crontab -l > "$PRW/crontab.backup2" 2>/dev/null
if [ -f "$PRW/backup.ok" ] && cmp -s "$PRW/crontab.backup2" "$PRW/crontab.backup"; then
  pw --config "$PRW/config-cron.yaml" cron install --every 1m --dry-run
  pw --config "$PRW/config-cron.yaml" cron install --every 1m --json
else
  echo "STOP: the crontab is not the verified backup; fix that (prw_restore) and skip M7 if unsure"
fi
```

Wait for two runs (about three minutes), then:

```sh
grep -E 'poll_(start|end)' "$XDG_STATE_HOME/pr-watch/log.jsonl" | tail -4
pw list --json | head -c 600; echo
pw cron uninstall --json; crontab -l 2>&1; crontab -l 2>/dev/null | cmp - "$PRW/crontab.backup"; echo "restored exit=$?"
```

Expected: `poll_start` and `poll_end` records for each cron run, `poll_end` without `interrupted`, no `tool_missing` and no `failed` entry for forgectl (so cron's `PATH` found forgectl and the credentials were readable under cron: forgectl reads `HOME`, which cron sets), `last_polled_at` of the watched PR moving, and the crontab restored (`cmp` silent; otherwise `prw_restore`).

- [ ] pass  - [ ] fail  - [ ] skip

## M8. `browse`

Needs a terminal. The store from the earlier steps holds one PR, one `unreachable` event and the poll records, which is enough to see every view. The state directory is the scratch one.

### M8.1 Start, keys and exit

```sh
pw browse; echo "exit=$?"
```

In Terminal.app, then again in iTerm2 (and in tmux, if you use it): the three views show (`1`, `2`, `3` or `tab`), `j`/`k` and the arrows move, `/` filters (type, `enter` keeps it, `escape` clears it), `s` cycles the status filter, `l` the label filter, `r` refreshes, `enter` focuses the detail pane and `escape` returns, `?` shows the help, `q` quits with exit 0. **To be observed**: colors follow the terminal's own palette (Textual's `ansi-dark` theme, no hard-coded background) in a light and a dark terminal profile; arrow keys, `ctrl+c` and the mouse (clicking a row) work; text is not garbled in Terminal.app's default profile (box drawing and the `<U+XXXX>` markers); resizing the window redraws.

- [ ] pass  - [ ] fail (list the terminals tried in the run record)

### M8.2 Exit paths restore the terminal

```sh
pw browse --focus "$PR"; echo "exit=$?"
```

Press `q`; then run it again and, from a second terminal tab, send `kill -TERM <pid>` to the PID shown by `ps -o pid=,command= -t <tty of the first tab>` (look the tty up with `tty` in the first tab). Expected: `--focus` opens on that PR; `q` exits 0; the SIGTERM exits 143 and the first terminal is usable afterwards (echo is on, the cursor is back, the alternate screen is gone: run `stty -a | head -3` and type a command). Repeat for `-HUP` (129). A broken terminal after any of them is a finding (`reset` fixes it).

- [ ] pass  - [ ] fail

### M8.3 Refusals

```sh
pw browse --focus 'github.com/none/none#1'; echo "unstored exit=$?"
pw browse --json; echo "json exit=$?"
pw browse < /dev/null; echo "no stdin tty exit=$?"
XDG_STATE_HOME="$PRW/empty-browse" pw browse; echo "empty store exit=$?"; ls -A "$PRW/empty-browse" 2>&1 | head -1
```

Expected: exit 10 for the unstored PR, 2 `bad_combination` for `--json`, 2 `no_terminal` for a redirected stdin, and the empty store opens an empty browser (quit with `q`, exit 0) that creates no state directory (`ls` reports it does not exist).

- [ ] pass  - [ ] fail

### M8.4 Read-only against a live poll

In one terminal: `pw browse`. In another, with the stand-in in `pass` mode: `pw poll --full --json`. Expected: the browser keeps showing data and refreshes within about 5 seconds, never shows `STALE` longer than a poll's write, and the poll is not slowed or blocked by the open browser (the browser opens `state.db` with `mode=ro` and a one second busy timeout).

- [ ] pass  - [ ] fail

## M9. Clean up

```sh
crontab -l 2>/dev/null | grep -c 'pr-watch' ; ps -ax -o pid=,command= | grep -c '[s]leep 600'
```

Expected: both print 0 (no managed block left, no leftover `sleep 600`; if the second is not 0, check with `prw_alive` and `prw_reap`, never a pattern kill). Your real crontab was backed up in `crontab.backup` and `crontab.backup2` inside the scratch directory: keep them if anything differs. Check that the crontab is the backup, then remove the scratch directory only if the check passed and `PRW` is what M0.2 made:

```sh
PRW_OK=1
if [ -f "$PRW/crontab.backup" ]; then
  crontab -l 2>/dev/null | cmp -s - "$PRW/crontab.backup" && echo "crontab matches the backup" || { echo "STOP: the crontab differs from the backup; run prw_restore or fix it, keep $PRW"; PRW_OK=0; }
fi
case $PRW in /tmp/prw.??????) ;; *) echo "STOP: unexpected PRW='$PRW'"; PRW_OK=0 ;; esac
if [ "$PRW_OK" = 1 ] && [ -n "$PRW" ] && [ -d "$PRW" ]; then rm -rf "$PRW" && echo removed; fi
unset PRW XDG_STATE_HOME UV_TOOL_DIR UV_TOOL_BIN_DIR PR
```

(`/tmp/prw.*` holds only this run: the state, the tool installs, the stand-ins and the outputs. If the check printed `STOP`, `unset` still clears the variables but nothing was deleted: remove the directory by hand once the crontab is right. If you skipped M6, there is no backup and no check.) Close the shell, or restore your `PATH`.

- [ ] pass  - [ ] fail

## What each issue comment asks for

| Source | Item | Steps |
|---|---|---|
| Issue body | install, including the browse extra | M1.1, M1.2 |
| Issue body | `poll` under cron | M6.5 to M6.8, M7 |
| Issue body | leases after a reboot, boot-time substitute | M4, M4.5, M4.6 |
| Issue body | process-group signalling | M3, M5.4 to M5.6 |
| Issue body | hook environment and stdio | M5.1 to M5.3 |
| Issue body | state directory permissions | M2.1 to M2.5 |
| Issue body | forgectl resolution on a cron PATH | M1.4, M6.4, M6.6, M7 |
| Issue body | `validate`, `browse` | M1.3 to M1.5, M8 |
| Issue body | golden `--json` output validated against `schemas/` | M1.6 (the test suite; no separate step) |
| Comment on #46 (gateway) | `os.waitid` missing: accept or add a fallback | M3.8 (decision recorded) |
| Comment on #12 (leases) | unknown holder takeover after expiry; boot time via sysctl | M4.3, M4.4, M4.5, M4.6 |
| Comment on #28 (hooks) | `/bin/sh -c`, `hooks-cwd`, cleaned env, `/dev/null` stdio, TERM then KILL after 5 s only while the leader is unreaped, descendants not killed | M5.1 to M5.6 |
| Comment on #29 (cron) | `crontab -l` and `-r` wording and status. `crontab -` with empty stdin is not exercised: pr-watch uses `crontab -r` for an empty table and never pipes empty stdin | M6.1, M6.8 |
| Comment on #29 | Full Disk Access and the fixed hint | M6.1 (decision recorded), M6.6 |
| Comment on #29 | 999-byte line limit on vixie 4.x | M6.7a |
| Comment on #29 | trailing-newline refusal, `crontab -` stdin | M6.7b, M6.5 |
| Comment on #29 | group-writable Homebrew directories | M6.4 |
| Comment on #29 | `. 'file' && ...` under cron's `/bin/sh`, default `PATH` | M6.2, M6.6 |

## Run record

Fill this in and commit it with the issue number when the run is done.

| Field | Value |
|---|---|
| Date | |
| Run by | |
| macOS version (`sw_vers`) | |
| Architecture | |
| Shell | |
| Terminals tried for `browse` | |
| Python version | |
| pr-watch version | |
| forgectl version | |
| Source (git URL and commit, or checkout) | |

| Step | Result (`PASS`, `FAIL`, `OBS`, `SKIP`) | Value observed or note |
|---|---|---|
| M0.1 to M0.3 setup | | |
| M1.1 install with the browse extra | | |
| M1.2 core install, `browse` refusal | | |
| M1.3 `validate` offline | | |
| M1.4 forgectl on an absolute `PATH` only | | |
| M1.5 `validate --probe` | | |
| M1.6 `make verify` (counts) | | |
| M2.1 modes of the state tree | | |
| M2.2 read-only commands create nothing | | |
| M2.3 unsafe modes refused | | |
| M2.4 symlinks | | |
| M2.5 `HOME` and `XDG_STATE_HOME` fallbacks | | |
| M3.2 forgectl is a group leader | | |
| M3.3 SIGTERM | | |
| M3.4 SIGHUP | | |
| M3.5 SIGINT | | |
| M3.6 group SIGKILL after 5 s | | |
| M3.7 call deadline | | |
| M3.8 descendant after leader exit (decision: accept or fallback) | | |
| M3.9 several calls in flight | | |
| M4.1 lease row (`unknown`) | | |
| M4.2 live lease | | |
| M4.3 crash, unknown holder alive | | |
| M4.4 takeover after expiry | | |
| M4.5 boot-id substitute values (decision: keep or implement) | | |
| M4.6 reboot with a long lease | | |
| M5.1 hook run | | |
| M5.2 environment, stdio, group | | |
| M5.3 relative `HOME` | | |
| M5.4 timeout, TERM then KILL | | |
| M5.5 descendant outliving the hook | | |
| M5.6 signals during a hook | | |
| M6.1 `crontab -l` wording, exit status, Full Disk Access | | |
| M6.2 env file and `. file &&` rehearsal | | |
| M6.3 install dry run | | |
| M6.4 forgectl resolution, Homebrew directories | | |
| M6.5 real install | | |
| M6.6 cron environment (`PATH`, `~/.local/bin`, `HOME`, `SHELL`) | | |
| M6.7 line limit, trailing newline | | |
| M6.8 uninstall, `crontab -r` path | | |
| M7 poll under cron | | |
| M8.1 `browse` keys and colors | | |
| M8.2 terminal restored on exit and signals | | |
| M8.3 `browse` refusals | | |
| M8.4 `browse` beside a live poll | | |
| M9 clean up | | |

Decisions to record:

- Gateway without `os.waitid` (M3.8): accept the residual risk, or file an issue for a fallback (kqueue `NOTE_EXIT` on the leader).
- Lease identity (M4.5): keep `unknown` for macOS, or file an issue to read `kern.bootsessionuuid` and `ps lstart` through the gateway.
- `cron install` failure hint (M6.1): name Full Disk Access, or not.
- Homebrew directories (M6.4): the documented way to place forgectl for cron.

Issues filed (number, title, step):

-

Overall: macOS checklist **passed** / **passed with findings** / **failed**. When it passes, comment on #32 with the run record and close it; plan.md Stage 6 and the 1.0 gate then drop "macOS checklist".
