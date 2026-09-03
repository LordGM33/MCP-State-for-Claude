# Module: vigia-red

Watches inbound network traffic and stays quiet unless something meaningful
changes. It learns the host's own normal level from its history instead of using
a threshold someone guessed, so it works on a busy server and on an idle one
without being told which is which.

It speaks up on two independent conditions:

- **Packets an order of magnitude above the median for that hour of day.**
  Comparing against one median for all hours makes the watcher deaf at night.
  Measured on a real host over 60 samples: ~1395 packets/min by day, ~170 at
  night — eight times less. Against the global median of 1221, an attack pushing
  1200 packets/min at 3am is seven times that hour's normal and still lands
  *below* the threshold. The baseline is therefore taken from the same hour ±1,
  falling back to the global median until that hour has five samples.
- **Outbound traffic starting to track inbound.** This is the one that matters,
  and it is deliberately **not** gated behind a volume threshold. Scanning that
  a firewall drops arrives and gets no answer, so bytes in stay high while bytes
  out stay flat. The moment out starts following in, the server is *answering*.
  A competent intruder is quiet — low volume, high response ratio — which is
  exactly the case a volume gate hides. The ratio is compared against this
  host's own normal ratio, above a small floor so near-idle traffic does not
  produce noise.

A watcher that reports every day gets ignored by the third one. This one is
designed to be silent for weeks — but silence has to come from nothing
happening, not from a threshold nothing can reach.

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

**The first runs are quiet by design.** It needs five samples in an hour band
before it will judge volume, and ten before it will judge the response ratio.
Until then it only accumulates. Expect roughly a day of silence on a fresh
install, and a week before the hour-of-day baselines are worth much.

**Upgrading from the first version discards the history once.** v1 stored a bare
packet count per sample with no hour and no byte figures, which cannot support
either of the tests above. Those samples are dropped on first run of v2 and the
watcher rebuilds from scratch. It happens once, not on every run.

**Thresholds are judgment, not proof.** Ten times the hourly normal, and three
times the usual response ratio, are choices — a spike at seven times the night
baseline stays silent. They are set to buy weeks of silence at the cost of
missing the subtlest cases. If this host ever sees a real incident, re-derive
them from what it actually looked like rather than from taste.

## Install

    bash modulos/vigia-red/instalar.sh

## Remove

    bash modulos/vigia-red/instalar.sh --desinstalar
