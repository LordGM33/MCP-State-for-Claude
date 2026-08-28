# Optional modules

The channel works without any of these. A module is something that reaches
outside the channel — another machine, a third-party service, the network —
and that not every deployment wants.

Each module ships a `MODULO.md` that states, before you install anything:

- **What it does**, in one paragraph.
- **What it requires**, including every credential and every permission, named
  exactly as the provider names them.
- **What it can reach** once installed, and just as importantly **what it
  cannot** — the boundary is part of the contract, not a footnote.
- **How to remove it**, because a module you cannot uninstall is not optional.

`instalar.sh` in each module prints that permission list and refuses to proceed
without an explicit confirmation. That is deliberate: a module that asks for a
credential should make you look at what you are handing over.

## Available

| Module | Reaches | Needs a credential |
|---|---|---|
| `scrum-drive` | Google Drive | yes — a service account key |
| `vigia-red` | nothing outside the host | no |

## Installing one

    bash modulos/<name>/instalar.sh

## Rules a module follows

1. **It never becomes a dependency of the channel.** Remove the module and the
   channel keeps working exactly as before.
2. **It gets the narrowest access that does the job**, and says so out loud.
   `scrum-drive` reads the database with `query_only` and writes to a single
   document that someone shared with it.
3. **It runs as the service user**, never as root, unless its `MODULO.md`
   explains why that is impossible.
4. **It fails loudly.** A module that breaks silently is worse than one that is
   not installed.
