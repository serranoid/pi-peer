# pi-peer

A local channel that lets **Claude Code sessions talk to pi** — and get answers back.

Claude Code keeps a small peer protocol for its own sessions: every live session
binds a Unix socket, publishes a record and an auth key, and `SendMessage` /
`ListAgents` speak it. It is undocumented and it has no way to include anything
that is not a Claude session.

`pi-peer` joins as one of those peers. After that, in any Claude session you can
say *"ask pi-second-brain how the backlog looks"* and the message reaches that pi
session, wakes it, and the answer comes back on the same channel.

```console
# from a pi session
$ pi-peer ls
pi-second-brain   307925  idle      /home/you/ia/pi-peer (this)
models             91624  idle      /home/you/dev/models
jobs               91633  idle      /home/you/dev/jobs

$ pi-peer ask models "How many workers are still running?"
sent to models (91624) from /run/user/1000/cc-socks/307925.sock

← models (22:20:52)
Two: the ingestion worker and the backfill from this morning.
```

```
# in the Claude session
❯ ask pi-second-brain how many items are left in the backlog
  ● pi-second-brain: 23 open, 4 blocked on the ROCm upgrade.
```

## Two pieces

| Piece | What it gives you |
|---|---|
| **The pi extension** (`extensions/pi-peer.ts`) | A live pi session binds its own inbox on start, so Claude can message *that* session and it wakes up with the message as a user turn. It also gives pi a `peer_message` tool to call others. |
| **The daemon** (`pi-peer start`) | A standing inbox with a fixed name, independent of any session: messages wait in `inbox.jsonl` until someone reads them. Useful under systemd, and what the CLI talks to. |

Both speak the same protocol, so either can be addressed. Run the daemon for a
stable address (`pi-home`), the extension for a session that answers by itself,
or both under different names.

## Install

Needs Python 3.9+ for the daemon, a pi installation for the extension, and at
least one running Claude Code session (the protocol borrows the pid-namespace id
from one, so start Claude first).

```sh
git clone https://github.com/serranoid/pi-peer
cd pi-peer

# the daemon
uv tool install --from . pi-peer       # or: pipx install .
pi-peer start                          # background, name from Herdr or the folder
pi-peer status

# the extension
ln -s "$PWD/extensions/pi-peer.ts" ~/.pi/agent/extensions/pi-peer.ts
```

The extension binds on the next pi session and reports its name at startup
(`pi-peer: reachable as pi-second-brain`).

### Under systemd

A standing inbox that survives restarts:

```sh
mkdir -p ~/.config/systemd/user
cp contrib/pi-peer@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pi-peer@pi-home
systemctl --user status pi-peer@pi-home
```

The instance name (`pi-home`) is what other sessions see in `ListAgents`. Use a
different instance per mailbox: `pi-peer@pi-vault`, `pi-peer@pi-work`.

## Names

The address has to tell sessions apart when you keep several open. The name is,
in order:

1. `PI_PEER_NAME`, if set;
2. the **Herdr space** the terminal is in — the label Herdr shows in its agents
   list — read from `hc get $HC_TERMINAL_ID`, prefixed `pi-`;
3. the folder's basename, prefixed `pi-`.

A name already taken by a live session gets the tab's name if that has one, else
a number (`pi-second-brain-2`), so two sessions in the same space stay apart.

The name is read again every 30 seconds: rename the space in Herdr and the
address follows, so it always matches what is on screen. Herdr registers a
terminal a moment after it starts, so the lookup retries for a few seconds
before falling back to the folder.

## Commands

| Command | What it does |
|---|---|
| `pi-peer start [--name N]` | Run the inbox in the background |
| `pi-peer daemon [--name N] [--cwd DIR]` | Same, in the foreground (what systemd runs) |
| `pi-peer stop` / `status` | Stop it; say where it is |
| `pi-peer ls [--json] [--all]` | Every live session on this machine |
| `pi-peer ask <name\|pid> "text" [--wait N]` | Send, then wait for the answer |
| `pi-peer send <name\|pid> "text"` | Send and return |
| `pi-peer inbox [-n N] [--json]` | What arrived, newest last |

From pi, two tools: `peer_message` (send, wait for the reply) and
`peer_sessions` (who is there). From the TUI, `/peers`.

## What is verified, and what is guessed

Checked against Claude Code **2.1.328** on Linux, end to end in both directions:

- the socket lives in `/run/user/<uid>/cc-socks/<pid>.sock`, mode 600, and the
  directory must be one of the paths Claude accepts
- the key file is `<pid>.<sha256(realpath(socket))>.key` and holds
  `{peerToken, procStart, pidDomain}`; `procStart` is field 22 of `/proc/<pid>/stat`
- a frame is one JSON object per line: an optional `{"type":"auth","token":…}`
  first, then `{"type":"user","message":{"role":"user","content":…},"from":…,"uuid":…,"session_id":…}`
- **the sender must be the process that owns the socket**: the receiver compares
  the connecting pid with the address in `from`. Sending from a helper process is
  dropped in silence, which is why the daemon sends and the CLI only asks it to.
- a reply comes back wrapped in
  `<cross-session-message from="uds:…" hop-chain="…" from-name="…" from-mode="…">text</cross-session-message>`;
  a frame without the wrapper only carries its address, so the name is looked up
  in the registry
- the receiving Claude is told by its own system prompt that a peer is **not its
  user**: it may act within its own permission settings, but a peer can never
  approve a prompt, change settings, or launder a permission denial

Guessed or fragile: the field list of the session record (`kind`, `entrypoint`,
`version`…), which of them `ListAgents` filters on, and the rate limits
(`[peer-guard]`) a receiver applies. If a Claude update changes any of it this
breaks quietly — check `pi-peer ls` and the daemon log first.

## Security

- Local only: a Unix socket in a 0700 directory, mode 600, same user.
- Neither piece runs what a message says. A message becomes a **user turn in the
  pi session**, wrapped in a warning that a peer is not the operator.
- An incoming message is a request from a peer, not an instruction from the
  person at the keyboard. Anything asking for permissions, secrets, deletions or
  configuration changes is reported to the human, not done.
- No network, no telemetry.

## License

MIT.
