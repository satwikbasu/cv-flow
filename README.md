# cv-flow

A self-hosted Telegram bot that finds jobs worth your time and tailors your resume to each one.

Once a day it searches job boards, ranks what it finds against your profile, and sends you a short
list over Telegram. Pick a job and it rewrites your LaTeX resume to fit that posting and sends back
the PDF along with a plain summary of what changed. It does not apply for you.

It runs on your own machine, with your own free API keys. Ubuntu, AGPL-3.0.

## Install

On a fresh Ubuntu 24.04 box:

```bash
curl -fsSL https://raw.githubusercontent.com/satwikbasu/cv-flow/main/scripts/bootstrap.sh | bash
```

That clones the latest release and runs the installer, which walks you through the config. Full
steps, including how to get the API keys, are in [docs/setup.md](docs/setup.md). Day-to-day use is
in [docs/usage.md](docs/usage.md).

## Before you run it

A few things worth knowing up front.

It reads public job boards (LinkedIn, Indeed, Naukri) through JobSpy. Boards do not love being
scraped, so you may hit rate limits or a temporary block, and heavy use can run up against their
terms of service. The defaults keep the volume modest; leave them there unless you know what you
are doing.

It runs on free LLM tiers, Mistral by default. Free tiers usually let the provider train on what
you send them, and the bot sends your profile and the job text. Use a provider you are comfortable
with, and point it at a paid endpoint if you would rather your data not be used that way; any
OpenAI-compatible API works.

The defaults are built for the Indian market: salary floors in lakhs per annum, Naukri as a board,
India and Remote as the locations, and Asia/Kolkata as the timezone. It works elsewhere, but you
will need to retune the locations, the salary numbers, and the boards.

Onboarding reads the text inside your resume file. A scanned PDF, which is really an image with no
text layer, will not parse; export a text-based PDF or a DOCX instead.

And to say it once more: it does not apply to jobs for you. You review every tailored resume and
send it yourself.

## License

Copyright (C) 2026 satwikbasu. Licensed under the GNU Affero General Public License v3.0 or later;
see [LICENSE](LICENSE). Security reports go through the process in [SECURITY.md](SECURITY.md).
