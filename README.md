# hermes-manager

Turns a Debian 13 server into a shared [Hermes Agent](https://github.com/NousResearch/hermes-agent)
server for a small organisation: one central Hermes install, one central policy, and one
isolated Linux account per person, each with their own private Hermes Webapp.

Status: in development. See `PLAN.md`.

## What is and is not protected

**Protected: people from each other.** Every person has their own Linux account. Their
chats, files, ChatGPT login, Webapp and sandbox belong to that account, and the operating
system keeps other accounts out:

- home directories are private (mode 700) and `/proc` hides other accounts' processes
  (hidepid, a prerequisite);
- a firewall table rejects any loopback connection to a person's programs from another
  account, so nobody can reach another person's Webapp, browser or other local services;
- people's accounts and their sandboxes cannot reach the networks you block (the tailnet
  automatically when Tailscale is installed);
- every Webapp asks for a username and password.

**Not protected:**

- **From the server admin.** Root can read everything on the machine that runs the agent.
- **From the model provider.** Conversations go to the provider you configure.
- **The policy against a determined person.** The pinned model, approvals and sandbox
  settings cannot be changed from the Webapp or the person's settings. But a signed-in
  person has a shell on the server as their own account (the Webapp's terminal) and can
  get around it. `hermes-manager check` reports when someone does. Their account is still
  isolated from everyone else.
- **A person from their own agent.** An agent misled by a web page can do whatever that
  person's account can do. Most tools run in a rootless Podman sandbox, but some run on
  the host as the person.
- **Plain HTTP.** Use HTTPS (the default for office-network access) so passwords and chats
  are encrypted.
