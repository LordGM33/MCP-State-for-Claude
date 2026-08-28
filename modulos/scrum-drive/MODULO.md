# Module: scrum-drive

Publishes the commitment register as a plain-text Google Doc, once a day. A
notebook or knowledge base that follows that document stays current without
anyone refreshing it, and people who cannot reach the channel — someone with no
account, or reading from a phone — get a consultable copy.

The channel remains the source of truth. This only writes a snapshot outwards.

## What it requires

**On the host**

| Requirement | Why |
|---|---|
| Read access to the channel database | to render the register |
| **Write access to the database's directory** | SQLite in WAL mode maintains its side files even for a `SELECT`. The reader sets `PRAGMA query_only=ON`, so it cannot alter data despite having that permission |
| A credential file readable by the service user | to authenticate against Drive |
| `google-api-python-client` and `google-auth` in the venv | the installer adds them |

**On Google Cloud**

| Requirement | Exact name |
|---|---|
| A project | any; the API is free at this volume |
| The Drive API enabled | `Google Drive API` |
| A service account | no IAM roles needed — those grant access to Cloud resources, which this does not use |
| A key for it | JSON |
| **OAuth scope** | `https://www.googleapis.com/auth/drive` |
| The target document shared with the service account | role **Editor** |

### Why the full `drive` scope, and what it does not mean

`drive.file` looks like the right choice — it is the narrow one — but it only
covers files **the application itself created**. A document shared *with* the
service account answers **404**, which reads like the file does not exist rather
than like a permission problem, and costs an hour to diagnose.

On a service account, the full `drive` scope means **its own Drive, which is
empty, plus whatever is explicitly shared with it**. It does not grant access to
the Drive of the person who created the document, or of anyone else in the
organisation. The blast radius is the set of documents you share, and nothing
else.

The alternative, if that still feels too broad: let the service account **create**
the document itself, and `drive.file` suffices. The cost is that the document
then belongs to the service account — it counts against its storage quota and
people reach it by link only.

## What it can reach

- The channel database, read-only in practice (`query_only`).
- Exactly one Google Doc: the id in `EVA_SCRUM_DOC_ID`, plus any other file
  someone shares with that service account.

## What it cannot do

- Write to the channel. It never opens an MCP connection and holds no token.
- Read any Drive file that has not been shared with its service account.
- Survive removal: uninstall it and the channel is unchanged.

## Risks worth knowing

**A service account key does not expire and does not rotate itself.** If it
leaks, it works until someone revokes it. Google disables keys it detects in
public repositories, which is a real safety net, but do not rely on it: keep the
JSON out of any repository and off shared drives.

**The published document is as sensitive as the register.** Anyone you share it
with reads every commitment, who owns it, and every time a date moved. That is
the point, but decide it deliberately.

**Organisations created recently** enforce Google's secure-by-default policies,
which block service account key creation through two separate constraints
(`iam.disableServiceAccountKeyCreation` and its `iam.managed.` counterpart).
Lifting one takes effect a few minutes later, not instantly.

## Install

    bash modulos/scrum-drive/instalar.sh

It asks for the credential path and the document id, prints the permission list
above, and refuses to continue without confirmation.

## Remove

    bash modulos/scrum-drive/instalar.sh --desinstalar

Stops and deletes the timer, the unit and the script. **It does not delete the
credential**, on purpose: removing a key is a decision with consequences outside
this host, so it tells you where it is and lets you do it.
