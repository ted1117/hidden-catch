from celery import Celery

from app.core.config import settings

celery_app = Celery(
    backend=settings.celery_result_backend, broker=settings.celery_broker_url
)

celery_app.conf.update(
    task_serializer="pickle",
    result_serializer="json",
    accept_content=["json", "pickle"],
)


celery_app.autodiscover_tasks(["app.worker"])
