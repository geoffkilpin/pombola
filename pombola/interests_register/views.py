from django.shortcuts import get_object_or_404, render
from django.http import HttpResponseRedirect
from django.core.urlresolvers import reverse
from django.views import generic

from django.db.models import Count

from pombola.interests_register.models import Entry
from pombola.interests_register.models import EntryLineItem
from pombola.interests_register.models import Category
from pombola.core.models import Organisation

class IndexView(generic.ListView):
    template_name = 'interests_register/index.html'
    context_object_name = 'context'
  
    def get_queryset(self):
        context = {}
        
        context['organisations'] = Organisation.objects.all()
        context['categories'] = Category.objects.all()
        if 'action' not in self.request.GET:
            return context
        
        if self.request.GET['action']=='orderby':
            context['action']='orderby'
            context['categoryname'] = get_object_or_404(Category,id=self.request.GET['category'])
            context['category'] = self.request.GET['category']
            
            #custom sql used due to NotImplementedError (Exception Value: annotate() distinct(fields) not implemented)
            #return Entry.objects.filter(person_id=483).order_by().distinct('person','release','category').annotate(c=Count('id'))
            context['orderby'] = Entry.objects.raw("SELECT max(id) as id, category_id, release_id, person_id, count(*) as c FROM \"interests_register_entry\" WHERE \"interests_register_entry\".\"category_id\" = %s GROUP BY category_id, release_id, person_id ORDER BY c DESC LIMIT 20",[self.request.GET['category']])
            return context
        
        if self.request.GET['action']=='groupby' and (self.request.GET['category']=='5' or self.request.GET['category']=='10'):
            context['action']='groupby'
            context['categoryname'] = get_object_or_404(Category,id=self.request.GET['category'])
            context['category'] = self.request.GET['category']
            context['groupby'] = Entry.objects.raw("SELECT max(\"interests_register_entrylineitem\".\"id\") as id, value, count(*) as c FROM \"interests_register_entrylineitem\" INNER JOIN \"interests_register_entry\" ON (\"interests_register_entrylineitem\".\"entry_id\" = \"interests_register_entry\".\"id\") WHERE (\"interests_register_entry\".\"category_id\" = %s AND \"interests_register_entrylineitem\".\"key\" = 'Source') GROUP BY value ORDER BY c DESC LIMIT 20",[self.request.GET['category']])
            return context
            
        if self.request.GET['action']=='groupby' and (self.request.GET['category']=='9'):
            context['action']='groupby'
            context['categoryname'] = get_object_or_404(Category,id=self.request.GET['category'])
            context['category'] = self.request.GET['category']
            context['groupby'] = Entry.objects.raw("SELECT max(\"interests_register_entrylineitem\".\"id\") as id, value, count(*) as c FROM \"interests_register_entrylineitem\" INNER JOIN \"interests_register_entry\" ON (\"interests_register_entrylineitem\".\"entry_id\" = \"interests_register_entry\".\"id\") WHERE (\"interests_register_entry\".\"category_id\" = %s AND \"interests_register_entrylineitem\".\"key\" = 'Sponsor') GROUP BY value ORDER BY c DESC LIMIT 20",[self.request.GET['category']])
            return context
        if self.request.GET['action']=='where':
            context['action']='where'
            context['categoryname'] = get_object_or_404(Category,id=self.request.GET['category'])
            context['category'] = self.request.GET['category']
            context['organisation'] = self.request.GET['organisation']
            #context['groupby'] = Entry.objects.raw("SELECT max(\"interests_register_entrylineitem\".\"id\") as id, value, count(*) as c FROM \"interests_register_entrylineitem\" INNER JOIN \"interests_register_entry\" ON (\"interests_register_entrylineitem\".\"entry_id\" = \"interests_register_entry\".\"id\") WHERE (\"interests_register_entry\".\"category_id\" = %s AND \"interests_register_entrylineitem\".\"key\" = 'Sponsor') GROUP BY value ORDER BY c DESC LIMIT 20",[self.request.GET['category']])
            
            #crude way to display interests in a table - almost certainly can be improved
            headers = EntryLineItem.objects.filter(entry__person__position__organisation__slug=self.request.GET['organisation']).filter(entry__category_id=self.request.GET['category']).distinct('key')
            context['headers']=['Person','Category','Year']
            context['declarations']=[]
            headercount=3
            headerindex={}
            for header in headers:
                context['headers'].append(header.key)
                headerindex[header.key]=headercount
                headercount+=1
            declarations = Entry.objects.filter(person__position__organisation__slug=self.request.GET['organisation']).filter(category_id=self.request.GET['category'])
            for declaration in declarations:
                row=['']*headercount
                row[0]=declaration.person.name
                row[1]=context['categoryname'].name
                row[2]=declaration.release.date.year
                for line in declaration.line_items.all():
                    row[headerindex[line.key]]=line.value
                context['declarations'].append(row)
            return context
            
class FilterView(generic.ListView):
    template_name = 'interests_register/filter.html'
    context_object_name = 'list_declarations'
  
    def get_queryset(self):
        if 'person_id' in self.request.GET and 'release' in self.request.GET and 'category' in self.request.GET:
            return Entry.objects.filter(person_id=self.request.GET['person_id']).filter(release_id=self.request.GET['release']).filter(category=self.request.GET['category'])
        elif 'category' in self.request.GET and 'source' in self.request.GET:
            return Entry.objects.filter(category=self.request.GET['category']).filter(line_items__value=self.request.GET['source'])
        else:
            return False
  
