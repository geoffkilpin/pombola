from django import forms
from django.forms.widgets import Textarea, Select
import datetime
from pombola.core.models import Place


class CountyPerformancePetitionForm(forms.Form):
    name = forms.CharField(
        error_messages={'required': 'You must enter a name'},
        widget=forms.TextInput(
            attrs={
                'required': 'required',
                'placeholder': 'Your name'}))
    email = forms.EmailField(
        error_messages={'required': 'You must enter a valid email address'},
        widget=forms.TextInput(
            attrs={
                'required': 'required',
                'placeholder': 'Your email address'}))

    def clean(self):
        # We want to provide a single helpful error message if both
        # name and email address aren't supplied, so override clean to
        # detect that condition
        cleaned_data = super(CountyPerformancePetitionForm,
                             self).clean()
        if not cleaned_data:
            raise forms.ValidationError(
                "You must specify a name and a valid email address")
        return cleaned_data


class CountyPerformanceSenateForm(forms.Form):
    comments = forms.CharField(
        label='Tell the senate what would like to see in your county',
        error_messages={'required': "You didn't enter a comment"},
        widget=Textarea(
            attrs={
                'rows': 5,
                'cols': 60,
                'required': 'required',
                'placeholder': 'Add any comments here'}))


class YouthEmploymentCommentForm(forms.Form):
    comments = forms.CharField(
        label='Give us your comments on the Bill',
        error_messages={'required': "You didn't enter a comment"},
        widget=Textarea(
            attrs={
                'rows': 5,
                'cols': 60,
                'required': 'required',
                'placeholder': 'Add any comments here'}))


class NamedModelChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.name

class YouthEmploymentSupportForm(forms.Form):

    constituencies = NamedModelChoiceField(
        queryset = Place.objects.filter(
                    kind__slug='constituency',
                    parliamentary_session__end_date__gte=datetime.date.today()
                   ).order_by('name'),
        empty_label = "Choose your constituency",
        error_messages={'required': "Please choose a constituency"}
    )

class YouthEmploymentInputForm(forms.Form):
    pass
