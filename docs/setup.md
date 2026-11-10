# Setting up cv-flow

This walks you from a fresh Ubuntu box to a running bot. It takes about ten minutes, most of it
spent getting a couple of API keys.

## What you need

- An Ubuntu 24.04 machine you control, x86_64 or arm64. A small cloud VM is plenty.
- A Telegram account.
- A Groq API key. The free plan is enough for everyday use and needs no card.

## Get your keys first

Three values, so have them ready before you run the installer.

**A Telegram bot token.** Open Telegram, message [@BotFather](https://t.me/BotFather), send
`/newbot`, and follow the prompts. It hands you a token that looks like `12345:AbCdEf...`. That is
your bot.

**Your numeric Telegram user id.** Message [@userinfobot](https://t.me/userinfobot) and it replies
with your id, a number like `87654321`. The bot will only talk to this id and ignore everyone else.

**A Groq API key.** Sign up at [console.groq.com](https://console.groq.com), open **API Keys**,
create one, and copy it. The default config uses Groq for both ranking and resume tailoring, so one
key is all you need to start.

## Run the installer

On the machine:

```bash
curl -fsSL https://raw.githubusercontent.com/satwikbasu/cv-flow/main/scripts/bootstrap.sh | bash
```

This clones the latest release into `~/cv-flow`, sets up a Python virtual environment, installs the
Tectonic LaTeX compiler, and then asks you for the three values above. It checks the Groq key
against the API as you type it, so a wrong paste gets caught right away. When it finishes it runs a
quick self-check so you can see everything is wired up.

The installer also registers a systemd user service and enables it, but does not start it yet; the
bot would only crash-loop until you have onboarded. It turns on lingering so the service can keep
running after you log out and across reboots.

If you are scripting the install or have no terminal to type into, pass `--non-interactive` and set
`CVFLOW_BOT_TOKEN` and `CVFLOW_LLM_KEY` in the environment. Pass `--no-services` to skip the
systemd setup.

## Onboard yourself

The bot needs to know who you are before it can rank jobs or tailor a resume. The easy way is to
start the bot once and send it your resume over Telegram:

```bash
systemctl --user start cvflow.service
```

Then message your bot `/onboard` and attach your resume as a PDF or DOCX. It reads your resume,
builds the profile and a compile-ready LaTeX master resume, and asks you to confirm a few details.
Remember that a scanned PDF (an image of a page) has no text to read, so export a text-based file.

You can also do it from the command line without the bot running:

```bash
cd ~/cv-flow && .venv/bin/cvflow onboard path/to/your-resume.pdf
```

If you would rather fill things in by hand, the files under `profile/` and `resume/master.tex` are
yours to edit directly. `config.example.yaml` documents every setting in `config.yaml`.

## Check it over and run

Run the self-check any time to confirm the config, profile, and resume all hold together:

```bash
cd ~/cv-flow && .venv/bin/python -m cvflow.checkup
```

Once that looks good, the bot is already running from the onboarding step above. To manage it:

```bash
systemctl --user restart cvflow.service    # restart after a config change
systemctl --user status cvflow.service     # is it running
journalctl --user -u cvflow.service -f      # watch the logs
```

Send your bot `/help` on Telegram and you are set. Day-to-day use is covered in
[usage.md](usage.md).
