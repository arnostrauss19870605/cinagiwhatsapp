from django.urls import path

from . import views

urlpatterns = [
    path("", views.questions, name="questions"),
    path("new/", views.question_edit, name="question_create"),
    path("<int:pk>/", views.question_results, name="question_results"),
    path("<int:pk>/edit/", views.question_edit, name="question_edit"),
    path("<int:pk>/export/", views.question_export, name="question_export"),
    path("<int:pk>/toggle/", views.question_toggle, name="question_toggle"),
    path("prize-draw/", views.prize_draw, name="prize_draw"),
    path("prize-draw/bonus/", views.bonus_entry, name="bonus_entry"),
    path("prize-draw/draw/", views.draw_winner, name="draw_winner"),
    path("prize-draw/export/", views.prize_draw_export, name="prize_draw_export"),
]
