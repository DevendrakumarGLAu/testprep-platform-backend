from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from exams.models import Exam, ExamCategory, ExamQuestion, ExamSection
from questionbank.models import Question, QuestionOption, Subject

# Simple, accurate practice questions: (text, [options], index of the correct option, explanation).
# They are demo content for trying out the platform, not real exam papers.
BANK = {
    "Maths": [
        ("What is 15% of 200?", ["20", "25", "30", "35"], 2, "15/100 x 200 = 30."),
        ("What is the simple interest on Rs 1000 at 5% per year for 2 years?", ["Rs 50", "Rs 100", "Rs 150", "Rs 200"], 1, "SI = 1000 x 5 x 2 / 100 = Rs 100."),
        ("What is the average of 10, 20, 30, 40 and 50?", ["25", "30", "35", "40"], 1, "The sum is 150 and 150 / 5 = 30."),
        ("If x + 5 = 12, what is x?", ["5", "6", "7", "8"], 2, "x = 12 - 5 = 7."),
        ("What is the area of a rectangle 8 cm long and 5 cm wide?", ["13 sq cm", "26 sq cm", "40 sq cm", "45 sq cm"], 2, "Area = length x width = 8 x 5 = 40."),
        ("What is the LCM of 4 and 6?", ["2", "10", "12", "24"], 2, "The smallest number divisible by both 4 and 6 is 12."),
    ],
    "GK": [
        ("What is the capital of India?", ["Mumbai", "New Delhi", "Kolkata", "Chennai"], 1, "New Delhi is the capital of India."),
        ("Who wrote the national anthem 'Jana Gana Mana'?", ["Bankim Chandra Chatterjee", "Rabindranath Tagore", "Sarojini Naidu", "Subramania Bharati"], 1, "It was written by Rabindranath Tagore."),
        ("Which is the largest planet in the solar system?", ["Earth", "Mars", "Jupiter", "Saturn"], 2, "Jupiter is the largest planet."),
        ("What is the national animal of India?", ["Lion", "Tiger", "Elephant", "Leopard"], 1, "The Bengal tiger is India's national animal."),
        ("Into which water body does the river Ganga finally flow?", ["Arabian Sea", "Indian Ocean", "Bay of Bengal", "Gulf of Kutch"], 2, "The Ganga empties into the Bay of Bengal."),
        ("Fundamental Rights are in which part of the Indian Constitution?", ["Part I", "Part II", "Part III", "Part IV"], 2, "Part III covers the Fundamental Rights."),
    ],
    "Reasoning": [
        ("What comes next in the series 2, 4, 8, 16, ?", ["24", "30", "32", "64"], 2, "Each number doubles, so 16 x 2 = 32."),
        ("Which is the odd one out: Apple, Banana, Carrot, Mango?", ["Apple", "Banana", "Carrot", "Mango"], 2, "Carrot is a vegetable; the others are fruits."),
        ("What comes next in the series A, C, E, G, ?", ["H", "I", "J", "K"], 1, "The letters skip one each time: A, C, E, G, I."),
        ("Pointing to a man, Ravi says, 'He is my mother's only brother.' How is the man related to Ravi?", ["Father", "Maternal uncle", "Cousin", "Grandfather"], 1, "His mother's brother is his maternal uncle."),
        ("What comes next in the series 3, 6, 9, 12, ?", ["14", "15", "16", "18"], 1, "Each number increases by 3."),
        ("Which direction is exactly opposite to North-East?", ["North-West", "South-East", "South-West", "West"], 2, "The opposite of North-East is South-West."),
    ],
    "English": [
        ("Choose the synonym of 'Rapid'.", ["Slow", "Quick", "Late", "Calm"], 1, "Rapid means quick."),
        ("Choose the antonym of 'Ancient'.", ["Old", "Modern", "Historic", "Aged"], 1, "The opposite of ancient is modern."),
        ("Choose the correct word: She ___ to school every day.", ["go", "goes", "going", "gone"], 1, "A singular subject takes 'goes' in the present tense."),
        ("One word for 'a person who loves books':", ["Bibliophile", "Philanthropist", "Pessimist", "Linguist"], 0, "A bibliophile loves books."),
        ("What is the plural of 'child'?", ["childs", "childes", "children", "childrens"], 2, "The plural of child is children."),
        ("Fill in the blank: He is good ___ mathematics.", ["in", "at", "on", "with"], 1, "We say 'good at' a subject."),
    ],
}
# Hindi versions of the Maths and GK demo questions, keyed by the English text. Questions without an entry stay English-only,
# which is also how a test can mix languages.
HINDI = {
    "What is 15% of 200?": ("200 का 15% कितना होता है?", ["20", "25", "30", "35"], "15/100 x 200 = 30."),
    "What is the simple interest on Rs 1000 at 5% per year for 2 years?": ("1000 रुपये पर 5% वार्षिक दर से 2 वर्ष का साधारण ब्याज कितना होगा?", ["50 रुपये", "100 रुपये", "150 रुपये", "200 रुपये"], "साधारण ब्याज = 1000 x 5 x 2 / 100 = 100 रुपये।"),
    "What is the average of 10, 20, 30, 40 and 50?": ("10, 20, 30, 40 और 50 का औसत क्या है?", ["25", "30", "35", "40"], "योग 150 है और 150 / 5 = 30।"),
    "If x + 5 = 12, what is x?": ("यदि x + 5 = 12 है, तो x का मान क्या है?", ["5", "6", "7", "8"], "x = 12 - 5 = 7।"),
    "What is the area of a rectangle 8 cm long and 5 cm wide?": ("8 सेमी लंबे और 5 सेमी चौड़े आयत का क्षेत्रफल कितना है?", ["13 वर्ग सेमी", "26 वर्ग सेमी", "40 वर्ग सेमी", "45 वर्ग सेमी"], "क्षेत्रफल = लंबाई x चौड़ाई = 8 x 5 = 40।"),
    "What is the LCM of 4 and 6?": ("4 और 6 का लघुत्तम समापवर्त्य (LCM) क्या है?", ["2", "10", "12", "24"], "वह सबसे छोटी संख्या जो 4 और 6 दोनों से विभाज्य हो, 12 है।"),
    "What is the capital of India?": ("भारत की राजधानी क्या है?", ["मुंबई", "नई दिल्ली", "कोलकाता", "चेन्नई"], "नई दिल्ली भारत की राजधानी है।"),
    "Which is the largest planet in the solar system?": ("सौरमंडल का सबसे बड़ा ग्रह कौन सा है?", ["पृथ्वी", "मंगल", "बृहस्पति", "शनि"], "बृहस्पति सबसे बड़ा ग्रह है।"),
    "What is the national animal of India?": ("भारत का राष्ट्रीय पशु कौन सा है?", ["शेर", "बाघ", "हाथी", "तेंदुआ"], "बंगाल टाइगर भारत का राष्ट्रीय पशु है।"),
    "Into which water body does the river Ganga finally flow?": ("गंगा नदी अंत में किस जलराशि में मिलती है?", ["अरब सागर", "हिंद महासागर", "बंगाल की खाड़ी", "कच्छ की खाड़ी"], "गंगा बंगाल की खाड़ी में गिरती है।"),
}

SECTION_ORDER = ["Maths", "GK", "Reasoning", "English"]


class Command(BaseCommand):
    help = "Create a demo exam (SSC) with Maths, GK, Reasoning and English sections and sample tests of each kind. Safe to re-run."

    @transaction.atomic
    def handle(self, *args, **opts):
        subjects = {name: Subject.objects.get_or_create(name=name)[0] for name in SECTION_ORDER}
        ssc, _ = ExamCategory.objects.get_or_create(
            name="SSC", defaults={"description": "Staff Selection Commission exams. Demo content for trying the platform."}
        )
        ssc.subjects.set(subjects.values())

        questions: dict[str, list[Question]] = {}
        for name, rows in BANK.items():
            questions[name] = []
            for text, options, correct, why in rows:
                q = Question.objects.filter(text=text, subject=subjects[name]).first()
                if not q:
                    q = Question.objects.create(
                        text=text, subject=subjects[name], difficulty="easy", marks=2, negative_marks="0.5",
                        explanation=why, source_info="demo seed",
                    )
                    for i, o in enumerate(options):
                        QuestionOption.objects.create(question=q, text=o, is_correct=i == correct, order=i)
                hi = HINDI.get(text)
                if hi and not q.text_hi:
                    q.text_hi, q.explanation_hi = hi[0], hi[2]
                    q.save()
                    for opt, t in zip(q.options.all(), hi[1]):
                        QuestionOption.objects.filter(pk=opt.pk).update(text_hi=t)
                questions[name].append(q)

        def make(title, kind, parts, minutes, subject=None, year=None, description=""):
            if Exam.objects.filter(title=title).exists():
                return False
            exam = Exam.objects.create(
                title=title, category=ssc, test_type=kind, subject=subject, year=year, description=description,
                duration_minutes=minutes, max_attempts=0, negative_marking_enabled=True, status=Exam.Status.PUBLISHED,
                published_at=timezone.now(), version=2, instructions="Read each question carefully. Each correct answer earns 2 marks; each wrong answer loses 0.5.",
            )
            for si, (section, qs) in enumerate(parts):
                sec = ExamSection.objects.create(exam=exam, name=section, order=si)
                for qi, q in enumerate(qs):
                    ExamQuestion.objects.create(section=sec, question=q, order=qi, marks=q.marks, negative_marks=q.negative_marks, question_version=q.version)
            exam.passing_marks = exam.total_marks * 40 / 100
            exam.save(update_fields=["passing_marks"])
            return True

        made = 0
        pick = lambda lo, hi: [(n, questions[n][lo:hi]) for n in SECTION_ORDER]  # noqa: E731
        made += make("SSC Full Test 1 (demo)", "full_test", pick(0, 4), 30)
        made += make("SSC Full Test 2 (demo)", "full_test", pick(2, 6), 30)
        for name in SECTION_ORDER:
            made += make(f"{name} only: practice set 1 (demo)", "sectional", [(name, questions[name])], 15, subject=subjects[name])
        sample = "A short demo paper in the style of a previous year paper. It is NOT an actual past paper."
        made += make("SSC CGL 2023 Tier 1: sample paper (demo)", "previous_year", pick(0, 2), 12, year=2023, description=sample)
        made += make("SSC CGL 2022 Tier 1: sample paper (demo)", "previous_year", pick(4, 6), 12, year=2022, description=sample)
        self.stdout.write(f"Demo ready: exam SSC with {made} new test(s). Sign in as a student and open My preparation.")
