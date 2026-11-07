"""Surveillance automatique : un cycle régulier par TMS, indépendant des autres."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.core.clock import Clock
from backend.schemas.tms_config import TmsConfig
from backend.services.cycle_runner import CycleRunner, CycleTrigger

logger = logging.getLogger(__name__)


class MonitoringScheduler:
    def __init__(
        self,
        configs: list[TmsConfig],
        runner: CycleRunner,
        session_factory: Callable[[], Session],
        clock: Clock,
    ) -> None:
        self._runner = runner
        self._session_factory = session_factory
        self._clock = clock
        self._scheduler = BackgroundScheduler(timezone=UTC)
        for config in configs:
            self._scheduler.add_job(
                self.run_tms,
                "interval",
                seconds=config.polling_interval_seconds,
                args=[config],
                id=config.id,
                next_run_time=datetime.now(UTC),
                max_instances=1,
                coalesce=True,
            )

    @property
    def running(self) -> bool:
        return self._scheduler.running

    def job_ids(self) -> list[str]:
        return [job.id for job in self._scheduler.get_jobs()]

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=True)

    def run_tms(self, config: TmsConfig) -> None:
        """Exécute un cycle ; une erreur est journalisée sans arrêter le planificateur."""
        try:
            with self._session_factory() as session:
                result = self._runner.run(
                    session, config, CycleTrigger.SCHEDULED, self._clock.now()
                )
            if result is None:
                logger.info("Cycle ignoré, déjà en cours : %s", config.id)
        except SQLAlchemyError as error:
            # Seul le type est journalisé : le message pourrait contenir la connexion.
            logger.error("Cycle impossible pour %s, base : %s", config.id, type(error).__name__)
        except Exception:
            logger.exception("Erreur inattendue pendant le cycle de %s", config.id)
