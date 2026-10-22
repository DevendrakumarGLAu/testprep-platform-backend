from django.contrib import admin

from .models import Batch, Exam, ExamCategory, ExamQuestion, ExamSection

admin.site.register([Batch, ExamCategory, Exam, ExamSection, ExamQuestion])
