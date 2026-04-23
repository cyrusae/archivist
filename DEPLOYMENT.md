# Deployment & Testing Guide: Archivist 📚

This guide covers how to set up, test, and deploy the Archivist bot, from your local machine to a K3s cluster.

---

## 1. Prerequisites

- **Python 3.14+** (Managed by [uv](https://docs.astral.sh/uv/))
- **PostgreSQL 14+** with the `pgvector` extension available
  (the bot issues `CREATE EXTENSION IF NOT EXISTS vector` on first connect,
  but the server must permit it — see [`DATABASE.md`](DATABASE.md) for
  platform-specific installation and a first-run checklist)
- **Google Gemini API Key** (Obtain from [Google AI Studio](https://aistudio.google.com/))
- **Discord Bot Token**
  - **Required Intents:** both must be enabled in the
  [Discord Developer Portal](https://discord.com/developers) under
  Bot → Privileged Gateway Intents:
  - **Message Content Intent** — to read message text
  - **Server Members Intent** — for role-based overrides (`ignore: true`, etc.)
  - **Permissions:** `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`.

---

## 2. Local Development Setup

### Install Dependencies

```bash
# Install uv if you haven't
curl -LsSf https://astral.sh/uv/install.sh | sh

# Sync environment
uv sync
```

### Configure

```bash
cp config/default.yaml config/config.yaml
```

Edit `config/config.yaml` with your `discord.token`, `gemini.api_key`, and `database` credentials.

### Database Setup

Ensure PostgreSQL is running and create the database:

```bash
psql -h localhost -U postgres -c "CREATE DATABASE archivist;"
```

*(The bot will automatically initialize the tables and seed tags on its first run.)*

### Running the Bot

```bash
uv run python bot.py
```

---

## 3. Testing

### Automated Tests

```bash
uv run pytest
```

61 tests cover the SSRF guard, message parser, formatter length limits, and YouTube URL patterns.

### Manual Verification

Post the following in a watched Discord channel to verify specific features:

1.  **Standard Link:** `https://example.com` (Verify summary, tags, and archive link).
2.  **Fiction/Narrative:** `https://archiveofourown.org/works/...` (Verify it appends full-work params and summarizes "vibe").
3.  **Image Attachment:** Upload a photo (Verify Gemini describes it and provides OCR/Alt-text).
4.  **Flags:** `https://example.com -p` (Verify privacy mode: no summary/tags).
5.  **Exclusion:** `https://discord.gg/invitecode` (Verify the bot ignores this).

---

## 4. K3s Deployment

### A. Create the Secret

The `archivist-secrets` Secret must exist in the `archivist` namespace **before**
applying the manifest. The `deployment.yaml` in this repo does not contain
secret values — never add them there.

**Option 1 — kubectl (quickest):**

```bash
kubectl create secret generic archivist-secrets \
  --namespace archivist \
  --from-literal=DISCORD_TOKEN='your-token' \
  --from-literal=GEMINI_API_KEY='your-key' \
  --from-literal=DB_PASSWORD='your-password'
```

**Option 2 — SealedSecrets (recommended for gitops):**

```bash
# Fill in secrets.example.yaml (never commit the filled-in version), then:
kubeseal --format yaml < secrets.example.yaml > sealed-secret.yaml
kubectl apply -f sealed-secret.yaml
```

`secrets.example.yaml` is a template in the repo root; `secrets.yaml` (any
filled-in copy) is listed in `.gitignore`.

---

### B. Build the Image

Archivist uses Playwright, so the image must include browser binaries.
```bash
docker build -t your-registry/archivist:latest .
docker push your-registry/archivist:latest
```

### C. Apply to Cluster

```bash
kubectl apply -f deployment.yaml
```

### D. Verify

```bash
kubectl get pods -n archivist
kubectl logs -f deployment/archivist -n archivist
```

---

## 5. Maintenance

### Snapshots

PDF snapshots are stored in the `/app/snapshots` directory within the container.

- In `deployment.yaml`, it is recommended to mount a **PersistentVolumeClaim** to this path if you wish to keep snapshots across pod restarts.
- To view a snapshot locally: `kubectl cp archivist-pod-name:/app/snapshots/snap_xxx.pdf ./local.pdf`

### Database Backups

Since this is a "Digital Librarian," you should periodically backup the Postgres StatefulSet:

```bash
kubectl exec -it statefulset/postgres -n archivist -- pg_dump -U archivist archivist > backup.sql
```
