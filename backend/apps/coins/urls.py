from rest_framework.routers import DefaultRouter

from apps.coins.views import CoinViewSet

router = DefaultRouter()
router.register("coins", CoinViewSet, basename="coin")

urlpatterns = router.urls
