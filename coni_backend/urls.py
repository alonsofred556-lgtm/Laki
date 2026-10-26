from django.urls import include, path, re_path

from core import views


urlpatterns = [
    path("api/", include("core.urls")),
    path("", views.frontend),
    re_path(r"^(?P<filename>(?:assets/.*|index\.html|wallet\.html|activity\.html|settings\.html|app\.js|wallet\.js|config\.js|styles\.css))$", views.frontend),
]
