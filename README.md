# insta-exit-node

Stand up a [Tailscale](https://tailscale.com) exit node on any Debian/Ubuntu VPS in about two minutes. Works with DigitalOcean, Hetzner, Vultr, Linode, AWS Lightsail — anything offering SSH access or cloud-init user-data.

Two flows, both running the same idempotent on-VM provisioner (`provision.py`):

| Flow | Use when |
|---|---|
| **SSH** | The VM already exists and you can SSH in |
| **cloud-init** | You want the VM to provision itself on first boot |

## Quickstart

1. Install [`uv`](https://docs.astral.sh/uv/).
2. [Set up Tailscale](#1-tailscale-setup) — auto-approver ACL, then an auth key tagged `tag:exit`.
3. `cp examples/.env.example examples/.env && chmod 600 examples/.env`, then fill it in.
4. Run the [SSH](#2a-ssh-flow) or [cloud-init](#2b-cloud-init-flow) flow.
5. [Use it](#3-use-the-exit-node) from any device on your tailnet.

**Prerequisites:** `uv` locally (plus `ssh`/`scp` for the SSH flow); a Debian/Ubuntu VM with `VERSION_CODENAME` in `/etc/os-release` and root or passwordless-sudo access; any Tailscale account.

## 1. Tailscale setup

### Auto-approver ACL

Exit nodes need approval before other devices can route through them. To auto-approve by tag, add this to your policy JSON under **Admin console → Access controls**:

```json
{
  "tagOwners": { "tag:exit": ["autogroup:admin"] },
  "autoApprovers": { "exitNode": ["tag:exit"] }
}
```

Without it, the node appears in the console but stays unusable until you approve it under **Machines → (node) → Edit route settings**.

> This is the minimum needed to make auto-approval work, and it is intentionally permissive — it does not restrict who may *use* the exit node or SSH into it. Beyond a personal tailnet, add `acls` and `ssh` blocks. See the [ACL docs](https://tailscale.com/kb/1018/acls).

### Auth key

**Admin console → Settings → Keys → Generate auth key**, with **Ephemeral** ✓ (the node self-removes when the VM dies) and **Tags** `tag:exit`. Mark it **Reusable** only for the SSH flow — for cloud-init, prefer single-use ([why](#auth-key-exposure-cloud-init)).

Then fill in `examples/.env` (or just export `TS_AUTHKEY`):

```bash
TS_AUTHKEY=tskey-auth-kXXXXXXXX-XXXXXXXXXXXX
EXIT_HOST=root@1.2.3.4        # SSH flow only
EXIT_HOSTNAME=insta-exit-1
EXIT_TAG=tag:exit
```

## 2a. SSH flow

Create a VM however you like — DigitalOcean example (`doctl compute ssh-key list` for the fingerprint):

```bash
doctl compute droplet create insta-exit-1 \
  --image ubuntu-24-04-x64 --size s-1vcpu-512mb-10gb --region nyc3 \
  --ssh-keys <fingerprint> --wait
```

Set `EXIT_HOST=root@<ip>` from the output, then:

```bash
./examples/run.sh
# or directly:
uv run bin/insta-exit-node ssh --host root@1.2.3.4 --hostname insta-exit-1 --tag tag:exit
```

This copies `provision.py` to the VM, installs Tailscale from the official apt repo, enables IP forwarding, runs `tailscale up`, and confirms the control plane actually approved the exit node:

```
[provision]   100.x.x.x  insta-exit-1  (exit node)
[provision] Exit node is advertised and approved — ready to use.
```

If the auto-approver ACL is missing, it waits ~30s for approval, then warns instead.

## 2b. cloud-init flow

Bakes `provision.py` and your auth key into a `#cloud-config` document the VM runs on first boot — you never SSH in.

```bash
uv run bin/insta-exit-node cloud-init --hostname insta-exit-1 --tag tag:exit > userdata.yaml

doctl compute droplet create insta-exit-1 \
  --image ubuntu-24-04-x64 --size s-1vcpu-512mb-10gb --region nyc3 \
  --ssh-keys <fingerprint> --user-data-file userdata.yaml --wait
```

Or use the example wrapper: `DO_SSH_KEY=<fingerprint> ./examples/cloud-init-digitalocean.sh`

The generated document writes `provision.py` and the auth key (mode `0600`, root-only), runs the provisioner, and shreds the key. Wait 60–90s after creation, then check the [admin console](https://login.tailscale.com/admin/machines).

## 3. Use the exit node

```bash
tailscale up --exit-node=insta-exit-1 --exit-node-allow-lan-access=false
curl https://api.ipify.org      # should show the VM's public IP

tailscale up --exit-node=       # stop routing through it
```

**Teardown:** destroy the VM at your provider. Because the key was ephemeral, the node disappears from the console within a few minutes.

## CLI reference

```
uv run bin/insta-exit-node ssh --host USER@IP [options]
uv run bin/insta-exit-node cloud-init [options]
```

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
| `--host USER@IP` | — | SSH target (`ssh` only, required) |
| `--ssh-key PATH` | — | SSH private key (`ssh` only) |

## Troubleshooting

```bash
ssh root@<ip> 'journalctl -u tailscaled -n 100'     # tailscaled logs
ssh root@<ip> 'cat /var/log/cloud-init-output.log'  # cloud-init flow
ssh root@<ip> 'tailscale status'
```

**Node never appears:** check the VM has outbound internet, the key is valid and unexpired, and `journalctl -u tailscaled` for errors.

**Node appears but can't be selected:** the provisioner catches this and ends with a `WARNING:` block instead of `Exit node is advertised and approved`. Add the `autoApprovers.exitNode` ACL rule, or approve manually under **Machines → (node) → Edit route settings**, then re-run the provisioner (it's idempotent) to confirm.

## Security notes

### Auth key exposure (cloud-init)

The key is embedded in the user-data document. `provision.py` shreds the on-disk copy, but **user-data stays readable from inside the VM** for the instance's lifetime:

```bash
curl http://169.254.169.254/metadata/v1/user-data   # DigitalOcean
```

Anything on the VM that can reach `169.254.169.254` can recover it. So: **use a single-use key for cloud-init** — it's consumed on first boot and a leaked copy is then worthless. If you need a reusable key, use the SSH flow, which pipes it over the SSH channel and never writes it to user-data. Firewall the metadata service after boot if your provider allows it.

### SSH flow is trust-on-first-use

The `ssh` subcommand uses `StrictHostKeyChecking=accept-new` — the same TOFU model `ssh` uses for new hosts. The exposure window is the few seconds between VM creation and the SCP, but an attacker who can intercept traffic to the VM's IP in that window captures the auth key. For high-assurance use, pre-seed `~/.ssh/known_hosts` with a fingerprint obtained out-of-band from your provider's console or API.

### Tailscale SSH is on by default

`tailscale up --ssh` means any tailnet member your ACL permits can SSH into the exit node. Tighten your `ssh` ACL block ([docs](https://tailscale.com/kb/1193/tailscale-ssh)) or pass `--no-ssh`.

## Choosing a provider

Bandwidth is the main cost driver:

| Provider | Included bandwidth | Notes |
|---|---|---|
| **Hetzner** | ~20 TB/mo | Best value EU/US; cheapest sustained throughput |
| **OVHcloud / Contabo** | Unmetered (throttled) | Good for high volume |
| **DigitalOcean** | 500 GB – 1 TB | Easiest DX; `doctl` is polished |
| **Vultr / Linode** | 1–2 TB | Comparable to DO |
| **AWS Lightsail** | 1–3 TB | Useful if you're already in AWS |

Serverless platforms (Lambda, Cloud Run, Vercel) can't hold a persistent WireGuard tunnel and won't work. Fly.io Machines do work ([guide](https://tailscale.com/kb/1132/flydotio)) but meter egress at ~$0.02/GB past 100 GB — expensive for an exit node.
