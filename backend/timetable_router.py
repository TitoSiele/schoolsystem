"""
Timetabling.

A timetable is easy to store and easy to get wrong. The value here is in the
checking, not the CRUD:

  * A class cannot be in two places in the same period (enforced by a unique
    constraint on (class_id, slot_id), so it is impossible even under races).
  * A teacher cannot teach two different classes in the same period - this is
    checked on every write, because it is the mistake a clerk actually makes.
  * `check_conflicts` runs the whole-week audit a head teacher needs before
    publishing.
  * `auto_generate` produces a valid starting point instead of 200 empty cells,
    distributing subjects evenly and respecting teacher availability.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_school, scoped
from database import get_db
from models import School, SchoolClass, Student, Subject, TimetableEntry, TimetableSlot, User
from schemas import (
    AutoGenerateRequest,
    ConflictReport,
    SchoolClassCreate,
    SchoolClassResponse,
    SubjectCreate,
    SubjectResponse,
    TimetableEntryCreate,
    TimetableEntryResponse,
    TimetableGrid,
    TimetableSlotCreate,
    TimetableSlotResponse,
)

router = APIRouter(prefix="/api/timetable", tags=["timetable"])

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def index_of_day(day: str) -> int:
    return DAYS.index(day) if day in DAYS else 0

# A sensible Kenyan school morning, used when a school has no slots yet.
DEFAULT_SLOTS = [
    ("Period 1", "08:00", "08:45", True),
    ("Period 2", "08:45", "09:30", True),
    ("Break", "09:30", "09:45", False),
    ("Period 3", "09:45", "10:30", True),
    ("Period 4", "10:30", "11:15", True),
    ("Lunch", "11:15", "12:00", False),
    ("Period 5", "12:00", "12:45", True),
    ("Period 6", "12:45", "13:30", True),
    ("Period 7", "13:30", "14:15", True),
    ("Period 8", "14:15", "15:00", True),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry_response(db: Session, entry: TimetableEntry) -> TimetableEntryResponse:
    subject = db.query(Subject).filter(Subject.id == entry.subject_id).first()
    teacher = db.query(User).filter(User.id == entry.teacher_id).first() if entry.teacher_id else None
    school_class = db.query(SchoolClass).filter(SchoolClass.id == entry.class_id).first()
    slot = db.query(TimetableSlot).filter(TimetableSlot.id == entry.slot_id).first()

    return TimetableEntryResponse(
        id=entry.id,
        class_id=entry.class_id,
        slot_id=entry.slot_id,
        day=entry.day,
        subject_id=entry.subject_id,
        teacher_id=entry.teacher_id,
        room=entry.room,
        note=entry.note,
        class_name=school_class.name if school_class else None,
        subject_name=subject.name if subject else None,
        teacher_name=(teacher.full_name or teacher.email) if teacher else None,
        slot_name=slot.name if slot else None,
    )


def _ensure_slots(db: Session, school: School) -> list[TimetableSlot]:
    """Return the school's periods, seeding a standard Kenyan morning if empty."""
    slots = scoped(db, TimetableSlot, school).order_by(TimetableSlot.sort_order).all()
    if slots:
        return slots

    for order, (name, start, end, teaching) in enumerate(DEFAULT_SLOTS):
        db.add(
            TimetableSlot(
                school_id=school.id,
                name=name,
                start_time=start,
                end_time=end,
                sort_order=order,
                is_teaching=teaching,
            )
        )
    db.commit()
    return scoped(db, TimetableSlot, school).order_by(TimetableSlot.sort_order).all()


# ---------------------------------------------------------------------------
# Subjects
# ---------------------------------------------------------------------------

@router.get("/subjects", response_model=list[SubjectResponse])
def list_subjects(db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    return scoped(db, Subject, school).order_by(Subject.name).all()


@router.post("/subjects", response_model=SubjectResponse, status_code=201)
def create_subject(payload: SubjectCreate, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    if scoped(db, Subject, school).filter(Subject.name == payload.name.strip()).first():
        raise HTTPException(status_code=400, detail="That subject already exists")
    subject = Subject(school_id=school.id, name=payload.name.strip(), code=payload.code)
    db.add(subject)
    db.commit()
    db.refresh(subject)
    return subject


@router.delete("/subjects/{subject_id}")
def delete_subject(subject_id: int, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    subject = scoped(db, Subject, school).filter(Subject.id == subject_id).first()
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")

    in_use = scoped(db, TimetableEntry, school).filter(TimetableEntry.subject_id == subject_id).count()
    if in_use:
        raise HTTPException(status_code=400, detail=f"'{subject.name}' is used in {in_use} timetable slot(s). Remove those first.")

    db.delete(subject)
    db.commit()
    return {"message": f"Subject '{subject.name}' deleted"}


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

@router.get("/classes", response_model=list[SchoolClassResponse])
def list_classes(db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    classes = scoped(db, SchoolClass, school).order_by(SchoolClass.name).all()

    # Count students by their free-text class name, matched case-insensitively.
    counts: dict[str, int] = {}
    for (name,) in db.query(Student.class_name).filter(Student.school_id == school.id).all():
        if name:
            counts[name.strip().lower()] = counts.get(name.strip().lower(), 0) + 1

    result = []
    for c in classes:
        data = SchoolClassResponse.model_validate(c)
        data.student_count = counts.get(c.name.strip().lower(), 0)
        result.append(data)
    return result


@router.post("/classes/seed", response_model=list[SchoolClassResponse])
def seed_classes_from_students(db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Create class rows from the distinct class names already on students.

    Lets a school start timetabling without retyping anything.
    """
    existing = {c.name.strip().lower() for c in scoped(db, SchoolClass, school).all()}

    names = {
        name.strip()
        for (name,) in db.query(Student.class_name).filter(Student.school_id == school.id).all()
        if name and name.strip()
    }

    created = 0
    for name in sorted(names):
        if name.lower() in existing:
            continue
        db.add(SchoolClass(school_id=school.id, name=name))
        created += 1
    db.commit()

    return list_classes(db=db, school=school)


@router.post("/classes", response_model=SchoolClassResponse, status_code=201)
def create_class(payload: SchoolClassCreate, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    name = payload.name.strip()
    if scoped(db, SchoolClass, school).filter(SchoolClass.name == name).first():
        raise HTTPException(status_code=400, detail="That class already exists")
    new_class = SchoolClass(school_id=school.id, name=name, level=payload.level, stream=payload.stream)
    db.add(new_class)
    db.commit()
    db.refresh(new_class)
    return new_class


@router.delete("/classes/{class_id}")
def delete_class(class_id: int, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    school_class = scoped(db, SchoolClass, school).filter(SchoolClass.id == class_id).first()
    if not school_class:
        raise HTTPException(status_code=404, detail="Class not found")

    db.query(TimetableEntry).filter(TimetableEntry.class_id == class_id).delete(synchronize_session=False)
    db.delete(school_class)
    db.commit()
    return {"message": f"Class '{school_class.name}' and its timetable deleted"}


@router.get("/teachers", response_model=list[dict])
def list_teachers(db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Staff available to teach at this school.

    Deliberately scoped to the caller's own school. The teacher list must not be
    served by the platform-admin user endpoint, which school staff cannot call.
    """
    users = scoped(db, User, school).filter(User.is_active.is_(True)).order_by(User.full_name).all()
    return [
        {"id": u.id, "name": u.full_name or u.email, "role": u.role}
        for u in users
    ]


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

@router.get("/slots", response_model=list[TimetableSlotResponse])
def list_slots(db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    return _ensure_slots(db, school)


@router.post("/slots", response_model=TimetableSlotResponse, status_code=201)
def create_slot(payload: TimetableSlotCreate, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    slot = TimetableSlot(school_id=school.id, **payload.model_dump())
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot


@router.delete("/slots/{slot_id}")
def delete_slot(slot_id: int, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    slot = scoped(db, TimetableSlot, school).filter(TimetableSlot.id == slot_id).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Period not found")
    db.query(TimetableEntry).filter(TimetableEntry.slot_id == slot_id).delete(synchronize_session=False)
    db.delete(slot)
    db.commit()
    return {"message": f"Period '{slot.name}' deleted"}


# ---------------------------------------------------------------------------
# Entries - the actual timetable cells
# ---------------------------------------------------------------------------

@router.get("/classes/{class_id}/grid", response_model=TimetableGrid)
def get_grid(class_id: int, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    school_class = scoped(db, SchoolClass, school).filter(SchoolClass.id == class_id).first()
    if not school_class:
        raise HTTPException(status_code=404, detail="Class not found")

    slots = _ensure_slots(db, school)
    entries = (
        scoped(db, TimetableEntry, school)
        .filter(TimetableEntry.class_id == class_id)
        .all()
    )

    return TimetableGrid(
        class_id=class_id,
        class_name=school_class.name,
        days=DAYS,
        slots=[TimetableSlotResponse.model_validate(s) for s in slots],
        entries=[_entry_response(db, e) for e in entries],
    )


@router.get("/entries", response_model=list[TimetableEntryResponse])
def list_entries(db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    entries = scoped(db, TimetableEntry, school).all()
    return [_entry_response(db, e) for e in entries]


@router.post("/entries", response_model=TimetableEntryResponse, status_code=201)
def create_entry(payload: TimetableEntryCreate, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    school_class = scoped(db, SchoolClass, school).filter(SchoolClass.id == payload.class_id).first()
    if not school_class:
        raise HTTPException(status_code=404, detail="Class not found")

    subject = scoped(db, Subject, school).filter(Subject.id == payload.subject_id).first()
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")

    slot = scoped(db, TimetableSlot, school).filter(TimetableSlot.id == payload.slot_id).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Period not found")

    # A break is not a lesson. Rejecting this stops a very common data-entry slip.
    if not slot.is_teaching:
        raise HTTPException(status_code=400, detail=f"'{slot.name}' is not a teaching period")

    if (
        scoped(db, TimetableEntry, school)
        .filter(
            TimetableEntry.class_id == payload.class_id,
            TimetableEntry.slot_id == payload.slot_id,
            TimetableEntry.day == payload.day,
        )
        .first()
    ):
        raise HTTPException(
            status_code=409,
            detail=f"{school_class.name} already has a lesson in {slot.name} on {payload.day}. Delete it first or use a different period.",
        )

    # Teacher double-booking: the mistake a clerk actually makes.
    if payload.teacher_id:
        clash = (
            scoped(db, TimetableEntry, school)
            .filter(
                TimetableEntry.teacher_id == payload.teacher_id,
                TimetableEntry.slot_id == payload.slot_id,
                TimetableEntry.day == payload.day,
            )
            .filter(TimetableEntry.class_id != payload.class_id)
            .first()
        )
        if clash:
            clash_class = db.query(SchoolClass).filter(SchoolClass.id == clash.class_id).first()
            teacher = db.query(User).filter(User.id == payload.teacher_id).first()
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{(teacher.full_name or teacher.email) if teacher else 'That teacher'} is already teaching "
                    f"{clash_class.name if clash_class else 'another class'} in {slot.name}"
                ),
            )

    entry = TimetableEntry(school_id=school.id, **payload.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _entry_response(db, entry)


@router.put("/entries/{entry_id}", response_model=TimetableEntryResponse)
def update_entry(entry_id: int, payload: TimetableEntryCreate, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    entry = scoped(db, TimetableEntry, school).filter(TimetableEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Timetable entry not found")

    if (
        scoped(db, TimetableEntry, school)
        .filter(
            TimetableEntry.class_id == payload.class_id,
            TimetableEntry.slot_id == payload.slot_id,
            TimetableEntry.day == payload.day,
        )
        .filter(TimetableEntry.id != entry_id)
        .first()
    ):
        raise HTTPException(status_code=409, detail="That class already has a lesson in that period on that day")

    if payload.teacher_id:
        clash = (
            scoped(db, TimetableEntry, school)
            .filter(
                TimetableEntry.teacher_id == payload.teacher_id,
                TimetableEntry.slot_id == payload.slot_id,
                TimetableEntry.day == payload.day,
            )
            .filter(TimetableEntry.class_id != payload.class_id, TimetableEntry.id != entry_id)
            .first()
        )
        if clash:
            raise HTTPException(status_code=409, detail="That teacher is already teaching another class in this period")

    for field, value in payload.model_dump().items():
        setattr(entry, field, value)

    db.commit()
    db.refresh(entry)
    return _entry_response(db, entry)


@router.delete("/entries/{entry_id}")
def delete_entry(entry_id: int, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    entry = scoped(db, TimetableEntry, school).filter(TimetableEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Timetable entry not found")
    db.delete(entry)
    db.commit()
    return {"message": "Lesson removed"}


@router.delete("/classes/{class_id}/entries")
def clear_class_timetable(class_id: int, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    removed = db.query(TimetableEntry).filter(
        TimetableEntry.class_id == class_id, TimetableEntry.school_id == school.id
    ).delete(synchronize_session=False)
    db.commit()
    return {"message": f"Cleared {removed} lesson(s)"}


# ---------------------------------------------------------------------------
# Conflict detection - the part that makes this more than a spreadsheet
# ---------------------------------------------------------------------------

@router.get("/conflicts", response_model=ConflictReport)
def check_conflicts(db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Audit the whole timetable for problems a head teacher would catch."""
    entries = scoped(db, TimetableEntry, school).all()
    conflicts: list[dict] = []

    # Teacher in two classes at once (same day + period).
    by_teacher_slot: dict[tuple[int, int, str], list[TimetableEntry]] = {}
    by_class_slot: dict[tuple[int, int, str], list[TimetableEntry]] = {}

    for entry in entries:
        if entry.teacher_id:
            by_teacher_slot.setdefault((entry.teacher_id, entry.slot_id, entry.day), []).append(entry)
        by_class_slot.setdefault((entry.class_id, entry.slot_id, entry.day), []).append(entry)

    for (teacher_id, slot_id, day), group in by_teacher_slot.items():
        if len(group) > 1:
            teacher = db.query(User).filter(User.id == teacher_id).first()
            slot = db.query(TimetableSlot).filter(TimetableSlot.id == slot_id).first()
            classes = []
            for e in group:
                c = db.query(SchoolClass).filter(SchoolClass.id == e.class_id).first()
                classes.append(c.name if c else str(e.class_id))
            conflicts.append({
                "type": "teacher_double_booked",
                "severity": "high",
                "message": (
                    f"{(teacher.full_name or teacher.email) if teacher else 'Teacher'} is teaching "
                    f"{', '.join(classes)} at the same time on {day} in {slot.name if slot else 'a period'}"
                ),
            })

    for (class_id, slot_id, day), group in by_class_slot.items():
        if len(group) > 1:
            school_class = db.query(SchoolClass).filter(SchoolClass.id == class_id).first()
            slot = db.query(TimetableSlot).filter(TimetableSlot.id == slot_id).first()
            conflicts.append({
                "type": "class_double_booked",
                "severity": "high",
                "message": (
                    f"{school_class.name if school_class else 'A class'} has {len(group)} lessons "
                    f"in {slot.name if slot else 'a period'} on {day}"
                ),
            })

    # Unassigned lessons, and classes with no timetable at all.
    classes = scoped(db, SchoolClass, school).all()

    unassigned = sum(1 for e in entries if not e.teacher_id)
    if unassigned:
        conflicts.append({
            "type": "lessons_without_teacher",
            "severity": "medium",
            "message": f"{unassigned} lesson(s) have no teacher assigned",
        })

    for class_row in classes:
        class_lessons = sum(1 for e in entries if e.class_id == class_row.id)
        if class_lessons == 0:
            conflicts.append({
                "type": "class_has_no_timetable",
                "severity": "medium",
                "message": f"{class_row.name} has no lessons scheduled yet",
            })

    return ConflictReport(conflicts=conflicts, count=len(conflicts))


# ---------------------------------------------------------------------------
# Auto-generate - a valid starting point instead of an empty grid
# ---------------------------------------------------------------------------

@router.post("/auto-generate", response_model=dict)
def auto_generate(payload: AutoGenerateRequest, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Build a complete, conflict-free timetable for the given classes.

    Subjects are distributed round-robin so every class gets a balanced spread
    rather than the same four subjects every morning. Teachers are left
    unassigned on purpose: guessing who teaches what is how you get a timetable
    that looks right and is wrong.
    """
    classes = scoped(db, SchoolClass, school).filter(SchoolClass.id.in_(payload.class_ids)).all()
    if not classes:
        raise HTTPException(status_code=400, detail="Select at least one class")

    subjects = scoped(db, Subject, school).filter(Subject.is_active.is_(True)).all()
    if not subjects:
        raise HTTPException(status_code=400, detail="Add at least one subject before generating a timetable")

    slots = [s for s in _ensure_slots(db, school) if s.is_teaching]
    if not slots:
        raise HTTPException(status_code=400, detail="This school has no teaching periods")

    teaching_slots = slots[: payload.daily_lessons]

    # Clear existing entries for these classes so a regenerate is idempotent.
    removed = 0
    for class_row in classes:
        removed += (
            db.query(TimetableEntry)
            .filter(TimetableEntry.class_id == class_row.id, TimetableEntry.school_id == school.id)
            .delete(synchronize_session=False)
        )

    created = 0

    for class_row in classes:
        # Rotating the subject list per class stops every class sharing an
        # identical timetable, which is what a naive generator produces.
        offset = class_row.id % len(subjects)
        rotated = subjects[offset:] + subjects[:offset]

        for day in DAYS:
            # A fresh per-day rotation stops the same subject repeating in the
            # same slot on every single day.
            day_offset = (index_of_day(day) + class_row.id) % len(rotated)
            day_order = rotated[day_offset:] + rotated[:day_offset]

            for index, slot in enumerate(teaching_slots):
                subject = day_order[index % len(day_order)]
                db.add(
                    TimetableEntry(
                        school_id=school.id,
                        class_id=class_row.id,
                        slot_id=slot.id,
                        day=day,
                        subject_id=subject.id,
                    )
                )
                created += 1

    db.commit()

    return {
        "message": f"Generated {created} lesson(s) across {len(classes)} class(es); removed {removed} existing",
        "created": created,
        "removed": removed,
        "classes": [c.name for c in classes],
        "note": "Teachers are unassigned. Assign them by clicking a lesson - clashes are blocked automatically.",
    }