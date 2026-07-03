"""Composition root: build the real services from config and run the bot.

``build_services`` wires storage, knowledge, discovery, the resume tailor and
the notifier into the two blocking run callables the handlers and scheduler
share. Nothing here talks to the network at build time — the LLM clients and
the notifier only reach out when a run actually happens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cvflow.analysis import JDAnalyzer
from cvflow.app.bot import build_application
from cvflow.app.commands import Services
from cvflow.app.notify import TelegramNotifier
from cvflow.app.scheduler import build_scheduler
from cvflow.config import Config, ProviderConfig, load_config
from cvflow.discovery import DiscoveryService
from cvflow.discovery.benchmark import build_fingerprint
from cvflow.discovery.skills import load_skill_profile
from cvflow.knowledge import KnowledgeBase
from cvflow.llm import ChatClient
from cvflow.resume import ResumeTailor, parse_master
from cvflow.runs import DiscoverOutcome, run_discover, run_tailor
from cvflow.storage import ApplicationStore

__all__ = ["build_services", "main"]


def _chat_client(cfg: ProviderConfig) -> ChatClient:
    return ChatClient(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        model=cfg.model,
        max_requests_per_minute=cfg.max_requests_per_minute,
        seed_field="random_seed" if cfg.provider == "mistral" else "seed",
    )


def build_services(config: Config) -> Services:
    store = ApplicationStore(config.storage.db_path)
    knowledge = KnowledgeBase.load(
        config.profile.knowledge_base_dir, config.storage.form_fields_path
    )
    distiller = _chat_client(config.llm.distillation)
    tailoring_client = _chat_client(config.llm.tailoring)
    skills_yaml = Path(config.profile.knowledge_base_dir) / "candidate_skills.yaml"
    candidate_skills, skill_synonyms = load_skill_profile(skills_yaml)
    discovery = DiscoveryService(
        store,
        search_terms=config.discovery.search_terms,
        locations=config.discovery.locations,
        sites=config.discovery.sites,
        results_wanted_per_site=config.discovery.results_wanted_per_site,
        hours_old=config.discovery.hours_old,
        exclude_title_keywords=config.preferences.exclude_title_keywords,
        min_ctc_lpa=config.preferences.min_ctc_lpa,
        country_indeed=config.discovery.country_indeed,
        linkedin_fetch_description=config.discovery.linkedin_fetch_description,
        distiller=distiller,
        fingerprint=build_fingerprint(
            prefs_text=knowledge.full_context(), prefer_roles=config.preferences.prefer_roles
        ),
        prefer_roles=config.preferences.prefer_roles,
        exclude_when=config.preferences.exclude_when,
        fit_weight=config.preferences.fit_weight,
        comp_weight=config.preferences.comp_weight,
        top_ctc_lpa=config.preferences.top_ctc_lpa,
        max_distill_per_cohort=config.discovery.max_distill_per_cohort,
        top_n_per_cohort=config.discovery.top_n_per_cohort,
        yoe_ceiling=config.preferences.yoe_have + config.preferences.yoe_buffer,
        reconsider_discovered=config.discovery.reconsider_discovered,
        candidate_skills=candidate_skills,
        skill_synonyms=skill_synonyms,
    )
    notify = TelegramNotifier(config.telegram.bot_token, config.telegram.authorized_user_id)

    # master_tex_path points at the master.tex FILE; parse_master wants its dir root.
    master_root = Path(config.resume.master_tex_path).parent
    fact_corpus = knowledge.full_context()
    if skills_yaml.exists():
        fact_corpus += "\n" + skills_yaml.read_text()
    tailor = ResumeTailor(
        tailoring_client,
        parse_master(master_root),
        max_projects=config.resume.max_projects,
        fact_corpus=fact_corpus,
        rephrase=config.resume.rephrase,
        disabled_sections=config.resume.disabled_sections,
    )
    analyzer = JDAnalyzer(distiller)
    summary_provider = (
        distiller if config.discovery.drop_summary_provider == "distillation"
        else tailoring_client
    )

    def _discover() -> DiscoverOutcome:
        return run_discover(
            store=store,
            discovery=discovery,
            notify=notify,
            retention_days=config.discovery.retention_days,
            report_drops=config.discovery.summarize_drops,
            summary_provider=summary_provider,
            log_dir=config.discovery.log_dir,
            summary_max_chars=config.discovery.drop_summary_max_chars,
            summary_samples=config.discovery.drop_summary_samples_per_bucket,
        )

    def _tailor(job_id: str) -> dict[str, str]:
        return run_tailor(
            job_id,
            store=store,
            tailor=tailor,
            output_dir=config.resume.output_dir,
            use_jd_analysis=config.resume.use_jd_analysis,
            analyzer=analyzer,
            notify=notify,
        )

    return Services(
        store=store,
        notify=notify,
        discover=_discover,
        tailor=_tailor,
        authorized_user_id=config.telegram.authorized_user_id,
    )


def main() -> None:  # pragma: no cover — the composition is tested; polling is not
    config = load_config("config.yaml")
    services = build_services(config)

    async def _start_scheduler(app: Any) -> None:
        build_scheduler(
            services,
            app.bot,
            chat_id=config.telegram.authorized_user_id,
            schedule=config.schedule,
        ).start()

    application = build_application(
        config.telegram.bot_token, services, post_init=_start_scheduler
    )
    application.run_polling()
