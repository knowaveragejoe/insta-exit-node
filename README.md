# insta-exit-node

Stand up a [Tailscale](https://tailscale.com) exit node in about two minutes. You need a Tailscale OAuth client and a provider API token. 

## Simple example
One command then creates the VM, provisions it, and registers it as approved:
 
```bash
uv run bin/insta-exit-node create --provider hetzner --region fsn1 --tag tag:exit
```

## Flexible usage

This tool supports three different flows depending on your use-case:

| Flow | Use when |
|---|---|
| **create / destroy** | You're on DigitalOcean or Hetzner and want the VM managed too |
| **SSH** | The VM already exists and you can SSH in |
| **cloud-init** | You want to hand the user-data document to another provider |

## Quickstart

1. Install [`uv`](https://docs.astral.sh/uv/).
2. [Set up Tailscale](#1-tailscale-setup) — auto-approver ACL, then an OAuth client tagged `tag:exit`.
3. `cp examples/.env.example examples/.env && chmod 600 examples/.env`, then fill it in.
4. [Create a VM](#2-create-a-vm), or provision [one you already have](#3-provision-a-vm-you-already-have).
5. [Use it](#4-use-the-exit-node) from any device on your tailnet.

**Prerequisites:** `uv` locally, and any Tailscale account. `create` also needs a DigitalOcean or Hetzner API token. The other flows need `ssh`/`scp` locally. They also need a Debian/Ubuntu VM with `VERSION_CODENAME` in `/etc/os-release`, and root or passwordless-sudo access.

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

> This is the minimum for auto-approval. It is deliberately permissive: it does not restrict who may *use* the exit node or SSH into it. Beyond a personal tailnet, add `acls` and `ssh` blocks. See the [ACL docs](https://tailscale.com/kb/1018/acls).

### OAuth client

**Admin console → Settings → OAuth clients → Generate**, with scopes `auth_keys` and `policy_file:read`, and tag `tag:exit`.

The tag matters. Keys minted through the API are owned by the tailnet. Tailscale requires a key's tags to match those of the client that created it.

Then fill in `examples/.env`:

```bash
TS_OAUTH_CLIENT_ID=kXXXXXXXXCNTRL
TS_OAUTH_CLIENT_SECRET=tskey-client-XXXXXXXXXXXX
EXIT_HOST=root@1.2.3.4        # SSH flow only
EXIT_HOSTNAME=insta-exit-1
EXIT_TAG=tag:exit
```

Each run mints its own auth key: single-use, ephemeral, pre-authorized, expiring in 10 minutes. There is no key to store, rotate, or revoke. The client secret stays on your machine. It never enters the user-data document, never reaches the VM, and is never logged.

> **Prefer to manage keys yourself?** Set `TS_AUTHKEY` (or pass `--authkey`) to skip minting. Generate the key under **Settings → Keys** with **Ephemeral** ✓ and **Tags** `tag:exit`.

## 2. Create a VM

Set a provider token, then:

```bash
export DIGITALOCEAN_TOKEN=dop_v1_...        # or HCLOUD_TOKEN for Hetzner
uv run bin/insta-exit-node create --provider digitalocean --tag tag:exit
```

```
[insta-exit-node] Policy OK (tag:exit auto-approved as an exit node).
[insta-exit-node] Minting auth key (single-use, ephemeral, expires in 600s)...
[insta-exit-node] Creating digitalocean VM 'insta-exit-1' (s-1vcpu-512mb-10gb, ubuntu-24-04-x64, nyc3)...
[insta-exit-node] Created insta-exit-1 (id 777) at 203.0.113.9
203.0.113.9
```

Only the IP goes to stdout, so `IP=$(uv run bin/insta-exit-node create ...)` works.

| Provider | Token env | Defaults |
|---|---|---|
| `digitalocean` | `DIGITALOCEAN_TOKEN` or `DO_API_TOKEN` | `nyc3` · `s-1vcpu-512mb-10gb` · `ubuntu-24-04-x64` |
| `hetzner` | `HCLOUD_TOKEN` | `nbg1` · `cx22` · `ubuntu-24.04` |

Override the defaults with `--region`, `--size`, and `--image`. If a value is wrong, the provider's error names the valid ones. `--ssh-key` (repeatable) installs a key already registered with the provider. Provisioning does not need it, but it lets you SSH in to debug.

### Teardown

```bash
uv run bin/insta-exit-node destroy --provider digitalocean --name insta-exit-1
```

`destroy` prompts before it deletes anything. `--yes` skips the prompt, and is required when stdin is not a terminal. Auth keys are ephemeral, so the Tailscale node leaves the console on its own within a few minutes.

Provider tokens stay on your machine. They never enter the user-data document and never reach the VM.

## 3. Provision a VM you already have

Use these flows for any provider `create` does not cover.

### SSH

Set `EXIT_HOST=root@<ip>` in `examples/.env`, then:

```bash
./examples/run.sh
# or directly:
uv run bin/insta-exit-node ssh --host root@1.2.3.4 --hostname insta-exit-1 --tag tag:exit
```

The command checks your tailnet policy and mints a key before touching the VM. It then copies `provision.py` over, installs Tailscale from the official apt repo, enables IP forwarding, and runs `tailscale up`. Last, it confirms the control plane approved the exit node:

```
[insta-exit-node] Policy OK (tag:exit auto-approved as an exit node).
[insta-exit-node] Minting auth key (single-use, ephemeral, expires in 600s)...
[provision]   100.x.x.x  insta-exit-1  (exit node)
[provision] Exit node is advertised and approved — ready to use.
```

If the auto-approver ACL is missing, the policy check fails before any VM work happens. The error includes the snippet to paste. Pass `--no-preflight` to skip the check.

### cloud-init

Bakes `provision.py` and your auth key into a `#cloud-config` user-data document. The VM runs it on first boot, so you never SSH in. `create` uses the same document. Run this subcommand directly to hand it to any other provider.

```bash
uv run bin/insta-exit-node cloud-init --hostname insta-exit-1 --tag tag:exit > userdata.yaml

doctl compute droplet create insta-exit-1 \
  --image ubuntu-24-04-x64 --size s-1vcpu-512mb-10gb --region nyc3 \
  --ssh-keys <fingerprint> --user-data-file userdata.yaml --wait
```

Or use the example wrapper: `DO_SSH_KEY=<fingerprint> ./examples/cloud-init-digitalocean.sh`

The document writes `provision.py` and the auth key (mode `0600`, root-only). It runs the provisioner, then shreds the key. Wait 60–90s after creation, then check the [admin console](https://login.tailscale.com/admin/machines).

## 4. Use the exit node

```bash
tailscale up --exit-node=insta-exit-1 --exit-node-allow-lan-access=false
curl https://api.ipify.org      # should show the VM's public IP

tailscale up --exit-node=       # stop routing through it
```

**Teardown:** run [`destroy`](#teardown), or remove the VM at any provider `create` does not cover.

## CLI reference

```
uv run bin/insta-exit-node create  --provider NAME [options]
uv run bin/insta-exit-node destroy --provider NAME --name VM [--yes]
uv run bin/insta-exit-node ssh --host USER@IP [options]
uv run bin/insta-exit-node cloud-init [options]
uv run bin/insta-exit-node authkey [options]     # mint a key, print it
```

`create`-only flags: `--name`, `--region`, `--size`, `--image`, `--ssh-key` (repeatable), `--wait-timeout`. Both provider commands take `--provider` and `--provider-token`.

Key resolution order: `--authkey` → `--authkey-file` → `$TS_AUTHKEY` → mint via the API. Progress messages go to stderr, so you can redirect `cloud-init` and `authkey` output safely.

| Flag | Default | Description |
|---|---|---|
| `--authkey KEY` | `$TS_AUTHKEY` | Tailscale auth key (skips minting) |
| `--authkey-file PATH` | — | File containing the auth key |
| `--oauth-client-id ID` | `$TS_OAUTH_CLIENT_ID` | OAuth client ID, used to mint keys |
| `--oauth-client-secret S` | `$TS_OAUTH_CLIENT_SECRET` | OAuth client secret |
| `--api-key KEY` | `$TS_API_KEY` | API access token, instead of an OAuth client |
| `--tailnet NAME` | `-` | Tailnet to operate on (`-` = the token's own) |
| `--key-expiry SECONDS` | `600` | Lifetime of a minted key |
| `--no-preflight` | off | Skip the tailnet policy check |
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

**Node never appears:** confirm the VM has outbound internet, and that the key is valid and unexpired. Check `journalctl -u tailscaled` for errors.

**Node appears but can't be selected:** the provisioner catches this. It ends with a `WARNING:` block instead of `Exit node is advertised and approved`. Add the `autoApprovers.exitNode` ACL rule, or approve manually under **Machines → (node) → Edit route settings**. Then re-run the provisioner — it's idempotent — to confirm.

## Security notes

### Auth key exposure (cloud-init)

The user-data document contains the key. `provision.py` shreds the on-disk copy. But **the document stays readable from inside the VM** for the instance's lifetime:

```bash
curl http://169.254.169.254/metadata/v1/user-data   # DigitalOcean
```

Minted keys are single-use and expire in 10 minutes. A key recovered this way is already spent, which is why minting is the default. A reusable `TS_AUTHKEY` of your own is a real exposure. Use the SSH flow instead: it pipes the key over the SSH channel and never writes it to the user-data document.

### SSH flow is trust-on-first-use

The `ssh` subcommand uses `StrictHostKeyChecking=accept-new` — the same TOFU model `ssh` uses for new hosts. The exposure window is the few seconds between VM creation and the SCP. An attacker who intercepts traffic to the VM's IP in that window captures the auth key. For high-assurance use, pre-seed `~/.ssh/known_hosts` with a fingerprint obtained out-of-band from your provider's console or API.

### Tailscale SSH is on by default

`tailscale up --ssh` means any tailnet member your ACL permits can SSH into the exit node. Tighten your `ssh` ACL block ([docs](https://tailscale.com/kb/1193/tailscale-ssh)) or pass `--no-ssh`.

## Choosing a provider

Bandwidth is the main cost driver:

| Provider | Included bandwidth | Notes |
|---|---|---|
| **Hetzner** | ~20 TB/mo | Best value EU/US; cheapest sustained throughput. `create` supported |
| **DigitalOcean** | 500 GB – 1 TB | Easiest DX. `create` supported |
| **OVHcloud / Contabo** | Unmetered (throttled) | Good for high volume; use the cloud-init flow |
| **Vultr / Linode** | 1–2 TB | Comparable to DO; use the cloud-init flow |
| **AWS Lightsail** | 1–3 TB | Useful if you're already in AWS; use the cloud-init flow |

Serverless platforms (Lambda, Cloud Run, Vercel) won't work: they can't hold a persistent WireGuard tunnel. Fly.io Machines do work ([guide](https://tailscale.com/kb/1132/flydotio)), but meter egress at ~$0.02/GB past 100 GB — expensive for an exit node.
