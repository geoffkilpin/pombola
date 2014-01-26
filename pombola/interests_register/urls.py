from django.conf.urls import patterns, url

from pombola.interests_register import views

urlpatterns = patterns('',
    url(r'^$', views.IndexView.as_view(), name='index'),
    url(r'^filter/$', views.FilterView.as_view(), name='filter'),
)
