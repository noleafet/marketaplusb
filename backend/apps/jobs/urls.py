from rest_framework.routers import DefaultRouter

from apps.jobs.views import JobRunViewSet, JobScheduleViewSet, JobSummaryView

router = DefaultRouter()
router.register("jobs/runs", JobRunViewSet, basename="job-run")
router.register("jobs/schedules", JobScheduleViewSet, basename="job-schedule")
router.register("jobs/summary", JobSummaryView, basename="job-summary")

urlpatterns = router.urls
