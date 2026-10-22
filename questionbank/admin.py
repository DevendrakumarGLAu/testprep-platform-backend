from django.contrib import admin

from .models import Question, QuestionOption, Subject, Tag, Topic

admin.site.register([Subject, Topic, Tag, Question, QuestionOption])
