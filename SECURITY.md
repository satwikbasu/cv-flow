# Security

cv-flow is a single-user bot you run on your own machine. It holds some things worth
protecting: your Telegram bot token, your LLM API keys, and a Telegram chat id that ties the
bot to you. This note covers how those are handled and how to report a problem.

## What the bot protects

- Tokens and API keys live in `config.yaml`, which is gitignored and never committed. The
  installer writes it `chmod 600`.
- The bot only answers the one Telegram account you set as `authorized_user_id`. Messages from
  anyone else are ignored.
- Job descriptions are fetched from the open web and treated as untrusted text. They are never
  executed, and the LaTeX that goes to the compiler runs through Tectonic with shell-escape off.

## What it does not do

There is no inbound network listener. The bot reaches out to Telegram and the LLM providers over
long-polling and HTTPS, so there is no port to expose and no web server to harden. It does not
apply to jobs, store payment details, or talk to anything you did not configure.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue. Use GitHub's private
vulnerability reporting on this repository (the **Security** tab, then **Report a vulnerability**).
Include what you found, how to reproduce it, and the version or commit you were on.

I will acknowledge a report within a few days and keep you posted while it is looked into. Once a
fix is out, you are welcome to credit, if you want it.
