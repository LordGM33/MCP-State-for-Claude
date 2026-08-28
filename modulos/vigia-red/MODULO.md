# Module: vigia-red

Watches inbound network traffic and stays quiet unless something meaningful
changes. It learns the host's own normal level from its history instead of using
a threshold someone guessed, so it works on a busy server and on an idle one
without being told which is which.

It speaks up on two conditions:

- **Packets an order of magnitude above the median** of what this host usually
  sees.
- **Outbound traffic starting to track inbound.** This is the one that matters.
  Scanning that a firewall drops arrives and gets no answer, so bytes in stay
  high while bytes out stay flat. The moment out starts following in, the server
  is *answering* — which is either real traffic or something that should not be
  reachable.

A watcher that reports every day gets ignored by the third one. This one is
designed to be silent for weeks.

## What it requires

| Requirement | Why |
|---|---|
| Read `/sys/class/net/<iface>/statistics/` | packet and byte counters |
| A writable state file | to remember the last 60 samples |
| Nothing else | no credentials, no network access, no database |

Optionally, to post its warning into the channel instead of the system log:

| Requirement | Why |
|---|---|
| A participant token | to write the alert |
| The channel's URL | where to send it |

**Give it its own identity.** Do not hand it a person's or a cowork's token: an
alert should be signed by the thing that raised it. Register a participant of
type `servicio` for it. Without a token it writes to the system log, which is a
perfectly good place for it if nobody is watching the channel at 4am anyway.

## What it can reach

- Kernel network counters on this host.
- The channel, only if you give it a token, and only to send messages.

## What it cannot do

- See traffic content. It counts packets and bytes; it does not inspect them.
- Change anything. It has no privileges beyond reading counters.

## Limits worth knowing

**It measures a window, not continuously.** By default it samples for two
minutes each hour, so a burst that starts and ends between samples is invisible.
That is a deliberate trade: continuous capture costs CPU and creates a log worth
attacking.

**The first runs are quiet by design.** It needs five samples before it has any
idea what normal looks like, and will not raise anything until then.

## Install

    bash modulos/vigia-red/instalar.sh

## Remove

    bash modulos/vigia-red/instalar.sh --desinstalar
