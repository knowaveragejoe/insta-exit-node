# insta-exit-node

Stand up a [Tailscale](https://tailscale.com) exit node on any Debian/Ubuntu VPS in under two minutes. Cloud-agnostic: works with DigitalOcean, Hetzner, Vultr, Linode, AWS Lightsail, or any provider that gives you SSH access or supports cloud-init user-data.

Two provisioning flows:

| Flow | When to use |
|---|---|
| **SSH** | You've already created a VM and can SSH into it |
| **cloud-init** | You want the VM to provision itself on first boot |

Both flows run the same on-VM provisioner (`provision.py`) which is idempotent — safe to re-run.

---

## Prerequisites

**On your machine (operator side):**

- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) — the only dependency:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- `ssh` and `scp` (for the SSH flow)
- A cloud provider account with a Debian/Ubuntu VM (or the ability to create one)

**On the VM:**
- Debian or Ubuntu (any version with a `VERSION_CODENAME` in `/etc/os-release`)
- Root or passwordless-sudo SSH access

**Tailscale:**
- A free or paid Tailscale account at [tailscale.com](https://tailscale.com)

---

## Step 1: Tailscale setup

### 1a. Add an auto-approver ACL rule

Exit nodes must be approved before other devices can use them. The easiest way is to auto-approve nodes that carry a specific tag.

Go to **Admin console → Access controls** and add these entries to your policy JSON:

```json
{
  "tagOwners": {
    "tag:exit": ["autogroup:admin"]
  },
  "autoApprovers": {
    "exitNode": ["tag:exit"]
  }
}
```

Save. Any node tagged `tag:exit` will be auto-approved as an exit node — no manual click required.

> **Minimum recommended, intentionally permissive.** The snippet above is the smallest ACL change needed to make auto-approval work; it does not constrain who on your tailnet can use the exit node or SSH into it. For anything beyond a personal tailnet you control end-to-end, you'll want to add `acls` and `ssh` blocks that restrict access to specific users/groups. See [Tailscale's ACL docs](https://tailscale.com/kb/1018/acls).

> **Note:** If you don't add this rule, your exit node will appear in the admin console but other devices won't be able to route through it until you manually approve it under **Machines → (node) → Edit route settings**.

### 1b. Create an auth key

Go to **Admin console → Settings → Keys → Generate auth key** and set:

| Setting | Value |
|---|---|
| Reusable | ✓ (lets you re-run the provisioner without a new key) |
| Ephemeral | ✓ (node disappears from console when the VM is destroyed) |
| Tags | `tag:exit` |

Copy the key — it looks like `tskey-auth-kXXXXXXXXXXXXXX-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX`.

---

## Step 2: Configure your secrets

Copy the example env file and fill it in:

```bash
cp examples/.env.example examples/.env
chmod 600 examples/.env
$EDITOR examples/.env
```

`examples/.env`:
```bash
TS_AUTHKEY=tskey-auth-kXXXXXXXXXXXXXX-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
EXIT_HOST=root@1.2.3.4        # for the SSH flow
EXIT_HOSTNAME=insta-exit-1
EXIT_TAG=tag:exit
```

Alternatively, export `TS_AUTHKEY` in your shell — the scripts will pick it up.

---

## Step 3a: SSH flow (provision an existing VM)

### Create the VM (DigitalOcean example)

```bash
doctl compute droplet create insta-exit-1 \
  --image ubuntu-24-04-x64 \
  --size s-1vcpu-512mb-10gb \
  --region nyc3 \
  --ssh-keys <your-key-fingerprint> \
  --wait
```

Find your SSH key fingerprint with `doctl compute ssh-key list`.

Get the droplet's public IP from the output, then set `EXIT_HOST=root@<ip>` in `examples/.env`.

### Run the provisioner

```bash
./examples/run.sh
```

Or call it directly:

```bash
export TS_AUTHKEY=tskey-auth-...
uv run bin/insta-exit-node ssh \
  --host root@1.2.3.4 \
  --hostname insta-exit-1 \
  --tag tag:exit
```

The script will:
1. Copy `provision.py` to the VM over SCP
2. SSH in, install Tailscale from the official apt repo, configure IP forwarding, and run `tailscale up`
3. Print `tailscale status` so you can confirm the node is up

Expected output (last few lines):
```
[provision] tailscale status:
[provision]   100.x.x.x  insta-exit-1  (exit node)
[provision] Provisioning complete.
```

---

## Step 3b: cloud-init flow (VM self-provisions on first boot)

The cloud-init flow bakes `provision.py` and your auth key into a `#cloud-config` document that the VM runs automatically when it first boots. You never need to SSH in.

### Generate the user-data document

```bash
export TS_AUTHKEY=tskey-auth-...
uv run bin/insta-exit-node cloud-init \
  --hostname insta-exit-1 \
  --tag tag:exit \
  > /tmp/exit-node-userdata.yaml
```

### What the document looks like

```yaml
#cloud-config
write_files:
  - path: /usr/local/sbin/provision.py
    permissions: '0755'
    owner: root:root
    encoding: b64
    content: <base64-encoded provision.py>
  - path: /root/.ts-authkey
    permissions: '0600'
    owner: root:root
    encoding: b64
    content: <base64-encoded auth key>
runcmd:
  - [python3, /usr/local/sbin/provision.py, --authkey-file, /root/.ts-authkey, --hostname, insta-exit-1, --tag, "tag:exit"]
  - shred -u /root/.ts-authkey 2>/dev/null || rm -f /root/.ts-authkey
```

The auth key is written to disk with mode `0600` (root-only), and shredded immediately after `tailscale up` completes. On a single-purpose disposable VM, this is acceptable; if you prefer not to write it to disk at all, use the SSH flow.

### Create the droplet with user-data (DigitalOcean)

```bash
doctl compute droplet create insta-exit-1 \
  --image ubuntu-24-04-x64 \
  --size s-1vcpu-512mb-10gb \
  --region nyc3 \
  --ssh-keys <your-key-fingerprint> \
  --user-data-file /tmp/exit-node-userdata.yaml \
  --wait
```

Or use the provided example script (edit `examples/.env` first):

```bash
DO_SSH_KEY=<fingerprint> ./examples/cloud-init-digitalocean.sh
```

Cloud-init runs in the background after the droplet is created. Wait 60–90 seconds, then check your [Tailscale admin console](https://login.tailscale.com/admin/machines) for the new node.

---

## Step 4: Use the exit node

On any device in your tailnet:

```bash
# Route all traffic through the exit node
tailscale up --exit-node=insta-exit-1 --exit-node-allow-lan-access=false

# Confirm your egress IP is the VM's public IP
curl https://api.ipify.org
```

To stop using the exit node:

```bash
tailscale up --exit-node=
```

---

## Teardown

Destroy the VM at your provider. Because the auth key was created **Ephemeral**, the Tailscale node disappears from the admin console automatically within a few minutes.

---

## CLI reference

```
uv run bin/insta-exit-node ssh --host USER@IP [options]
uv run bin/insta-exit-node cloud-init [options]
```

**Shared options (both subcommands):**

| Flag | Default | Description |
|---|---|---|
| `--authkey KEY` | `$TS_AUTHKEY` | Tailscale auth key |
| `--authkey-file PATH` | — | File containing the auth key |
| `--hostname NAME` | system hostname | Tailscale node hostname |
| `--tag TAG` | — | Advertise tag (repeatable) |
| `--advertise-route CIDR` | — | Subnet route to advertise (repeatable) |
| `--no-ssh` | ssh on | Disable Tailscale SSH |
| `--no-ipv6-forwarding` | ipv6 on | Disable IPv6 forwarding |
| `--reset` | off | Pass `--reset` to `tailscale up` |

**SSH-only options:**

| Flag | Description |
|---|---|
| `--host USER@IP` | SSH target (required) |
| `--ssh-key PATH` | Path to SSH private key |

---

## Debugging

**SSH flow:**
```bash
# Re-run the provisioner (idempotent)
./examples/run.sh

# Check tailscaled logs on the VM
ssh root@<ip> 'journalctl -u tailscaled -n 100'
```

**cloud-init flow:**
```bash
# Check cloud-init output (runs before tailscaled starts)
ssh root@<ip> 'cat /var/log/cloud-init-output.log'

# Check tailscaled logs
ssh root@<ip> 'journalctl -u tailscaled -n 100'

# Check tailscale status
ssh root@<ip> 'tailscale status'
```

**Exit node not appearing in Tailscale admin console:**
- Confirm the VM has outbound internet access
- Check that the auth key is valid and not expired
- Look for errors in `journalctl -u tailscaled`

**Exit node appears but can't be selected by other devices:**
- Verify your ACL has the `autoApprovers.exitNode` rule (Step 1a)
- Or manually approve under **Machines → (node) → Edit route settings**

---

## Security considerations

This is a small tool with a small attack surface, but a few tradeoffs are worth understanding before you deploy.

### Cloud-init exposes the auth key via the metadata service

In the cloud-init flow, the auth key is embedded (base64) in the user-data document. Although `provision.py` shreds the on-disk copy at `/root/.ts-authkey` after `tailscale up` completes, **the original user-data remains readable from inside the VM** via the cloud metadata service for the lifetime of the instance — e.g. on DigitalOcean:

```bash
curl http://169.254.169.254/metadata/v1/user-data
```

Anything on the VM that can reach `169.254.169.254` (any process, any future code execution, anyone who reads cloud-init logs) can recover the key.

**Recommendations:**

- **Use a single-use (non-reusable) auth key for the cloud-init flow.** It gets consumed by `tailscale up` on first boot; a leaked copy afterwards is useless for registering new nodes. The README earlier recommends "Reusable" for convenience when re-running the provisioner — that convenience only applies to the SSH flow.
- If you need reusable keys with cloud-init, prefer the SSH flow instead: it pipes the key over the SSH channel and never writes it into user-data.
- On providers that support it, disable or firewall the metadata service after first boot.

### SSH flow uses trust-on-first-use

`bin/insta-exit-node ssh` connects with `StrictHostKeyChecking=accept-new`, which accepts whatever host key the server presents on the first connection and pins it thereafter. This is the same TOFU model `ssh` uses by default for new hosts.

The window of risk is small (the few seconds between droplet creation and the SCP) but non-zero: an attacker who can intercept traffic to the VM's IP during that window — a malicious upstream, a hijacked floating IP, an attacker who has compromised your provider's control plane — could capture the auth key.

**Recommendations:**

- For high-assurance use, pre-seed `~/.ssh/known_hosts` with the VM's host key fingerprint obtained out-of-band (most providers expose this in their web console or via an API).
- For everyday personal use, TOFU is generally acceptable; just be aware that the auth key is what's at stake on a successful MITM.

### Tailscale SSH is enabled by default

The provisioner runs `tailscale up --ssh`, which means any tailnet member your ACL allows can SSH into the exit node. Combined with a permissive default ACL, this is broader access than you probably want. Either tighten your `ssh` ACL block (see Tailscale's [SSH ACL docs](https://tailscale.com/kb/1193/tailscale-ssh)) or pass `--no-ssh` to disable it entirely.

---

## Choosing a provider

For an exit node the main cost driver is bandwidth. Rough guide:

| Provider | Included bandwidth | Notes |
|---|---|---|
| **Hetzner** | ~20 TB/month | Best value for EU/US; cheapest sustained throughput |
| **OVHcloud / Contabo** | Unmetered (throttled) | Good for high-volume use |
| **DigitalOcean** | 500 GB – 1 TB/droplet | Easiest DX; `doctl` CLI is polished |
| **Vultr / Linode** | 1–2 TB/instance | Comparable to DO |
| **AWS Lightsail** | 1–3 TB/instance | Useful if you're already in AWS |

**A note on "serverless" platforms:** Lambda, Cloud Run, and Vercel functions are unsuitable — they're request-scoped and can't maintain a persistent WireGuard tunnel. Fly.io Machines technically work (Tailscale has [a guide](https://tailscale.com/kb/1132/flydotio)), but egress is metered at ~$0.02/GB after a 100 GB allowance, which is expensive for a personal exit node. Stick with a standard VPS unless you have a specific reason to use Fly.
