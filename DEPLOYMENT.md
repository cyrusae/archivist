# Deployment & Testing Guide: Archivist 📚

This guide covers how to set up, test, and deploy the Archivist bot, from your local machine to a K3s cluster.

---

## 1. Prerequisites

- **Python 3.12+** (Managed by [uv](https://docs.astral.sh/uv/))
- **PostgreSQL** (Local or via Docker)
- **Google Gemini API Key** (Obtain from [Google AI Studio](https://aistudio.google.com/))
- **Discord Bot Token**
  - **Required Intents:** `Server Members`, `Message Content`.
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

Run the parser unit tests to ensure URL extraction and flags are working:

```bash
uv run python tests/test_parser.py
```

### Manual Verification

Post the following in a watched Discord channel to verify specific features:

1.  **Standard Link:** `https://example.com` (Verify summary, tags, and archive link).
2.  **Fiction/Narrative:** `https://archiveofourown.org/works/...` (Verify it appends full-work params and summarizes "vibe").
3.  **Image Attachment:** Upload a photo (Verify Gemini describes it and provides OCR/Alt-text).
4.  **Flags:** `https://example.com -p` (Verify privacy mode: no summary/tags).
5.  **Exclusion:** `https://discord.gg/invitecode` (Verify the bot ignores this).

---

## 4. K3s Deployment

### A. Build the Image

Archivist uses Playwright, so the image must include browser binaries.
```bash
docker build -t your-registry/archivist:latest .
docker push your-registry/archivist:latest
```

### B. Configure Secrets

Open `deployment.yaml` and update the `Secret` section with your base64-encoded tokens or use a tool like `sops` / `sealed-secrets`.

*Note: The bot also supports direct environment variables if you prefer to inject them via your CI/CD pipeline.*

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
