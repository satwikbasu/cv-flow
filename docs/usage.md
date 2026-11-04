# Using cv-flow

Once the bot is running and you have onboarded, everything happens in your Telegram chat with it.
Here is the shape of a normal day and the commands you have.

## The daily digest

At the time you set (08:00 by default), the bot searches the job boards, ranks what it finds
against your profile, and sends you a short list. Each job shows its title, company, how old the
posting is, and where it ranked. Below the list is a row of buttons so you can act without typing.

If a board was unreachable that day, the digest says so rather than quietly dropping it. Every so
often the bot also posts a short "still running" note so you know it is alive; that interval is the
`heartbeat_interval_minutes` setting.

## Picking a job

To tailor your resume for a job, tap its **Tailor** button, or type `/tailor 2` for the second job
in the last list. A bare `2` works too. The bot rewrites your LaTeX resume to fit that posting,
compiles it, and sends back the PDF along with a plain-language summary of what it changed and why.
It never invents facts about you; if the posting wants something your profile does not cover, the
summary tells you instead of making it up.

To hide a job you are not interested in, tap **Skip** or type `/skip 2`. You can skip several at
once: `/skip 2 4` or `/skip 2-4`.

You review and submit every application yourself. The bot does not apply for you.

## Commands

- `/discover` runs a job hunt right now instead of waiting for the daily one.
- `/tailor N` tailors your resume for job N from the last list.
- `/skip N` hides job N. Ranges and lists work: `/skip 2 4`, `/skip 2-4`.
- `/jobs [open|tailored|all]` shows your backlog, freshly numbered. With no argument it shows the
  open jobs; `tailored` shows ones you have already tailored, `all` shows everything.
- `/status` shows a count of how many jobs are in each state.
- `/onboard` starts onboarding again; attach a resume to rebuild your profile.
- `/help` lists the commands.

The numbers in a command always refer to the last list the bot showed you, so run `/jobs` first if
you are not sure what is what.

## How jobs age out

A job you have not tailored drops off the backlog after about three weeks (the `retention_days`
setting, 21 by default). This keeps the list to things still worth applying for. A job you have
tailored stays as history and does not expire.

## Tuning what you see

Most of how the bot behaves lives in `config.yaml`: which boards and search terms it uses, your
salary floor and years of experience, how much weight fit gets versus pay, and which role families
to surface first. `config.example.yaml` explains each setting. After you change it, restart the
bot:

```bash
systemctl --user restart cvflow.service
```

If the bot ever goes quiet or behaves oddly, the self-check is the first place to look:

```bash
cd ~/cv-flow && .venv/bin/python -m cvflow.checkup
```
