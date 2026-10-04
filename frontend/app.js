let editingStudentId = null;  // tracks whether the modal is in add or edit mode
// from the backend (see frontend/config.js).
function apiUrl(path) {
    return api(path);
}

let currentUser = null;
let currentSchool = null;
let importRows = [];      // staged rows from the last upload
let photoStudentId = null;


// ============================================================================
// AUTH - gates the whole app
// ============================================================================

function showLoginScreen() {
    document.getElementById("login-screen").style.display = "flex";
    document.getElementById("app-shell").style.display = "none";
}


function showApp() {
    document.getElementById("login-screen").style.display = "none";
    document.getElementById("app-shell").style.display = "flex";

    // System Administration is only for the platform operator (you), never for
    // a school's own admin.
    document.getElementById("nav-admin").style.display = isPlatformAdmin() ? "flex" : "none";

    if (currentSchool) {
        const nameEl = document.getElementById("school-name");
        if (nameEl) nameEl.textContent = currentSchool.name;
        const planEl = document.getElementById("school-plan");
        if (planEl) planEl.textContent = `${currentSchool.plan} plan - ${currentSchool.student_count}/${currentSchool.max_students ?? "∞"} students`;
    } else if (currentUser && currentUser.is_platform_admin) {
        // A platform admin has no school; showing a school name would be a lie.
        document.getElementById("school-name").textContent = "All Schools";
        document.getElementById("school-plan").textContent = "Platform operator";
    }
}


async function checkSession() {
    try {
        const response = await fetch(apiUrl("/api/auth/me"));
        if (!response.ok) {
            showLoginScreen();
            return;
        }
        currentUser = await response.json();

        // A platform admin has no school of their own, so skip /api/school for
        // them - calling it would just produce a 400 in the console.
        if (currentUser && currentUser.is_platform_admin) {
            currentSchool = null;
        } else {
            const schoolRes = await fetch(apiUrl("/api/school"));
            currentSchool = schoolRes.ok ? await schoolRes.json() : null;
        }

        if (currentSchool?.subscription_status === "cancelled" || currentSchool?.subscription_status === "expired") {
            showLoginScreen();
            return;
        }

        showApp();
    } catch (error) {
        showLoginScreen();
    }
}


function setFormError(elementId, message) {
    const box = document.getElementById(elementId);
    if (!box) return;
    box.textContent = message || "";
    box.style.display = message ? "block" : "none";
}


async function submitLogin(event) {
    event.preventDefault();
    setFormError("login-error", "");

    const btn = document.getElementById("login-btn");
    btn.disabled = true;
    btn.textContent = "Signing in...";

    // A platform admin has no school of their own, so /api/school legitimately
    // rejects them. That is not an error - just leave currentSchool null.
    async function refreshProfile() {
        if (currentUser && currentUser.is_platform_admin) {
            currentSchool = null;
            return;
        }
        const schoolRes = await fetch(apiUrl("/api/school"));
        currentSchool = schoolRes.ok ? await schoolRes.json() : null;
    }

    try {
        const response = await fetch(apiUrl("/api/auth/login"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                email: document.getElementById("login_email").value.trim(),
                password: document.getElementById("login_password").value
            })
        });

        if (!response.ok) {
            setFormError("login-error", await readApiError(response, "Could not sign in."));
            return;
        }

        currentUser = await response.json();
        document.getElementById("login-form").reset();

        await refreshProfile();

        showApp();
    } catch (error) {
        setFormError("login-error", "Could not reach the server.");
    } finally {
        btn.disabled = false;
        btn.textContent = "Sign in";
    }
}


async function logout() {
    try {
        await fetch(apiUrl("/api/auth/logout"), { method: "POST" });
    } catch (error) {
        // even if the call fails, drop the UI to the login screen
    }
    currentUser = null;
    currentSchool = null;
    showLoginScreen();
}


function openRegisterModal() {
    setFormError("register-error", "");
    document.getElementById("register-modal").style.display = "flex";
}


function closeRegisterModal() {
    document.getElementById("register-modal").style.display = "none";
}


async function submitRegister(event) {
    event.preventDefault();
    setFormError("register-error", "");

    const btn = document.getElementById("register-btn");
    btn.disabled = true;
    btn.textContent = "Creating account...";

    try {
        const response = await fetch(apiUrl("/api/auth/register-school"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: document.getElementById("reg_school_name").value.trim(),
                code: document.getElementById("reg_school_code").value.trim().toUpperCase(),
                email: document.getElementById("reg_school_email").value.trim() || null,
                admin_name: document.getElementById("reg_admin_name").value.trim() || null,
                admin_email: document.getElementById("reg_admin_email").value.trim(),
                admin_password: document.getElementById("reg_admin_password").value,
                plan: "trial"
            })
        });

        if (!response.ok) {
            setFormError("register-error", await readApiError(response, "Could not create the account."));
            return;
        }

        closeRegisterModal();
        alert("School account created. Please sign in with your new admin email.");
        document.getElementById("login_email").value = document.getElementById("reg_admin_email").value.trim();
    } catch (error) {
        setFormError("register-error", "Could not reach the server.");
    } finally {
        btn.disabled = false;
        btn.textContent = "Create account";
    }
}


// ============================================================================
// BULK IMPORT
// ============================================================================

function openImportModal() {
    setFormError("import-error", "");
    importRows = [];
    document.getElementById("import_file").value = "";
    document.getElementById("import-step-upload").style.display = "block";
    document.getElementById("import-step-review").style.display = "none";
    document.getElementById("import-modal").style.display = "flex";
}


function closeImportModal() {
    document.getElementById("import-modal").style.display = "none";
}


function backToUploadStep() {
    document.getElementById("import-step-upload").style.display = "block";
    document.getElementById("import-step-review").style.display = "none";
}


function downloadImportTemplate() {
    const csv = "Admission No,First Name,Last Name,Class,Parent,Phone,Balance\n" +
                "ADM-001,John,Kiprotich,Form 1A,Paul Kiprotich,0722000001,45000\n" +
                "ADM-002,Mary,Achieng,Form 1A,Sam Achieng,0722000002,38500\n";
    const blob = new Blob([csv], { type: "text/csv" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "student_import_template.csv";
    link.click();
    URL.revokeObjectURL(link.href);
}


async function uploadImportFile() {
    const input = document.getElementById("import_file");
    const file = input.files[0];

    if (!file) {
        setFormError("import-error", "Choose a file first.");
        return;
    }

    setFormError("import-error", "");
    const btn = document.getElementById("import-upload-btn");
    btn.disabled = true;
    btn.textContent = "Reading file...";

    try {
        const form = new FormData();
        form.append("file", file);

        const response = await fetch(apiUrl("/api/v1/documents/universal-upload"), {
            method: "POST",
            body: form
        });

        if (!response.ok) {
            setFormError("import-error", await readApiError(response, "Could not read that file."));
            return;
        }

        const staged = await response.json();

        if (!staged.length) {
            setFormError("import-error", "No student rows were found in that file. Check the column headings.");
            return;
        }

        renderImportPreview(staged);
    } catch (error) {
        setFormError("import-error", "Could not reach the server.");
    } finally {
        btn.disabled = false;
        btn.textContent = "Upload and Preview";
    }
}


function renderImportPreview(staged) {
    importRows = staged;

    const needsReview = staged.filter(r => (r.error_flags || []).length > 0).length;
    document.getElementById("import-summary").innerHTML =
        `Found <strong>${staged.length}</strong> student row${staged.length === 1 ? "" : "s"}.` +
        (needsReview
            ? ` <span style="color:#b45309;">${needsReview} row${needsReview === 1 ? "" : "s"} missing required fields and will be skipped.</span>`
            : ` All rows look complete.`);

    const headers = ["Row", "Admission No", "First", "Last", "Class", "Parent", "Phone", "Balance", "Status"];
    const rows = staged.map(r => {
        const d = r.normalized_data || {};
        const flagged = (r.error_flags || []).length > 0;
        return `
            <tr style="${flagged ? "background:#fffbeb;" : ""}">
                <td>${r.row_number}</td>
                <td>${escapeHtml(d.admission_number || "—")}</td>
                <td>${escapeHtml(d.first_name || "—")}</td>
                <td>${escapeHtml(d.last_name || "—")}</td>
                <td>${escapeHtml(d.class_name || "—")}</td>
                <td>${escapeHtml(d.parent_name || "—")}</td>
                <td>${escapeHtml(d.parent_phone || "—")}</td>
                <td>${d.fee_balance != null ? escapeHtml(String(d.fee_balance)) : "—"}</td>
                <td>${flagged
                    ? `<span class="badge warning">Missing ${escapeHtml(r.error_flags.join(", "))}</span>`
                    : `<span class="badge success">OK</span>`}</td>
            </tr>`;
    }).join("");

    document.getElementById("import-preview").innerHTML = `
        <table>
            <thead><tr>${headers.map(h => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead>
            <tbody>${rows}</tbody>
        </table>`;

    document.getElementById("import-step-upload").style.display = "none";
    document.getElementById("import-step-review").style.display = "block";
}


async function commitImport() {
    // Only rows without error flags can be committed.
    const importable = importRows.filter(r => (r.error_flags || []).length === 0);

    if (!importable.length) {
        setFormError("import-error", "None of these rows are complete enough to import.");
        return;
    }

    const btn = document.getElementById("import-commit-btn");
    btn.disabled = true;
    btn.textContent = "Importing...";

    try {
        const response = await fetch(apiUrl("/api/v1/documents/staging/commit"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ staging_ids: importable.map(r => r.id), reviewed_by: currentUser?.email })
        });

        if (!response.ok) {
            setFormError("import-error", await readApiError(response, "The import could not be completed."));
            return;
        }

        const result = await response.json();
        closeImportModal();
        loadStudents();
        alert(`Imported ${result.committed} student${result.committed === 1 ? "" : "s"}.`);
    } catch (error) {
        setFormError("import-error", "Could not reach the server.");
    } finally {
        btn.disabled = false;
        btn.textContent = "Import into Students";
    }
}


// ============================================================================
// STUDENT PHOTOS
// ============================================================================

function openPhotoModal(studentId, photoUrl) {
    photoStudentId = studentId;
    setFormError("photo-error", "");
    document.getElementById("photo_file").value = "";

    const preview = document.getElementById("photo-preview");
    if (photoUrl) {
        preview.src = photoUrl;
        preview.style.display = "block";
    } else {
        preview.removeAttribute("src");
        preview.style.display = "none";
    }

    document.getElementById("photo-modal").style.display = "flex";
}


function closePhotoModal() {
    document.getElementById("photo-modal").style.display = "none";
    photoStudentId = null;
}


async function uploadPhoto() {
    const file = document.getElementById("photo_file").files[0];

    if (!file) {
        setFormError("photo-error", "Choose an image file first.");
        return;
    }

    const btn = document.getElementById("photo-upload-btn");
    btn.disabled = true;
    btn.textContent = "Uploading...";

    try {
        const form = new FormData();
        form.append("file", file);

        const response = await fetch(apiUrl(`/students/${photoStudentId}/photo`), {
            method: "POST",
            body: form
        });

        if (!response.ok) {
            setFormError("photo-error", await readApiError(response, "The photo could not be uploaded."));
            return;
        }

        closePhotoModal();
        loadStudents();
    } catch (error) {
        setFormError("photo-error", "Could not reach the server.");
    } finally {
        btn.disabled = false;
        btn.textContent = "Upload Photo";
    }
}


// Start the auth check as soon as the page is ready.
document.addEventListener("DOMContentLoaded", checkSession);


function showPage(pageId, clickedItem = null) {

    const pages = document.querySelectorAll(".page");
    pages.forEach(page => page.classList.remove("active-page"));

    const selectedPage = document.getElementById(pageId);
    if (selectedPage) {
        selectedPage.classList.add("active-page");
    }

    const navItems = document.querySelectorAll(".nav-item");
    navItems.forEach(item => item.classList.remove("active"));

    if (clickedItem) {
        clickedItem.classList.add("active");
    }

    const titles = {
        dashboard: ["Dashboard", "Overview of school fee collection"],
        students: ["Students", "Manage students and their information."],
        fees: ["Fee Accounts", "Monitor student fee balances."],
        payments: ["Payments", "Record and manage school fee payments."],
        reconciliation: ["Payment Reconciliation", "Match incoming payments with student accounts."],
        reports: ["Reports", "School financial reports and analysis."],
        billing: ["Billing", "Your SchoolPay subscription and invoices."],
        admin: ["System Administration", "Manage every school and user in SchoolPay."],
        timetable: ["Timetable", "Build and check the weekly teaching timetable."],
        "timetable-setup": ["Timetable Setup", "Subjects, classes and the school day."],
        mpesa: ["M-Pesa Payments", "Request payments by phone and reconcile the daily till statement."]
    };

    if (titles[pageId]) {
        document.getElementById("page-title").textContent = titles[pageId][0];
        document.getElementById("page-subtitle").textContent = titles[pageId][1];
    }

    window.scrollTo({ top: 0, behavior: "smooth" });

    if (pageId === "students") {
        loadStudents();
    }
}


function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}


async function loadStudents() {

    const tbody = document.getElementById("students-tbody");
    if (!tbody) return;

    tbody.innerHTML = `<tr><td colspan="6">Loading...</td></tr>`;

    try {
        const response = await fetch(apiUrl(`/students`));

        if (!response.ok) {
            throw new Error("Failed to load students");
        }

        const students = await response.json();

        if (students.length === 0) {
            tbody.innerHTML = `<tr><td colspan="6">No students yet. Click "+ Add Student" to add one.</td></tr>`;
            return;
        }

        tbody.innerHTML = students.map(s => `
            <tr>
                <td>
                    <div class="student">
                        ${s.photo_url
                            ? `<img class="student-avatar" src="${escapeHtml(s.photo_url)}" alt="" style="object-fit:cover;">`
                            : `<div class="student-avatar">${escapeHtml((s.first_name?.[0] || "") + (s.last_name?.[0] || ""))}</div>`}
                        <strong>${escapeHtml(s.first_name)} ${escapeHtml(s.last_name)}</strong>
                    </div>
                </td>
                <td>${escapeHtml(s.admission_number)}</td>
                <td>${escapeHtml(s.class_name)}</td>
                <td>${escapeHtml(s.parent_name)}</td>
                <td>KSh ${Number(s.fee_balance).toLocaleString()}</td>
                <td>
                    <button class="small-btn" onclick="openPhotoModal(${s.id}, ${s.photo_url ? `'${escapeHtml(s.photo_url)}'` : "null"})">📷</button>
                    <button class="small-btn" onclick="openEditStudentModal(${s.id})">Edit</button>
                    <button class="small-btn btn-danger" onclick="deleteStudent(${s.id})">Delete</button>
                </td>
            </tr>
        `).join("");

    } catch (error) {
        console.error("Error loading students:", error);
        tbody.innerHTML = `<tr><td colspan="6">Error loading students. Is the server running?</td></tr>`;
    }
}


function openAddStudentModal() {
    editingStudentId = null;
    document.getElementById("student-modal-title").textContent = "Add Student";
    document.getElementById("student-form").reset();
    document.getElementById("student-modal").style.display = "flex";
}


async function openEditStudentModal(studentId) {

    try {
        const response = await fetch(apiUrl(`/students/${studentId}`));
        if (!response.ok) throw new Error("Failed to load student");
        const student = await response.json();

        editingStudentId = studentId;
        document.getElementById("student-modal-title").textContent = "Edit Student";
        document.getElementById("admission_number").value = student.admission_number;
        document.getElementById("admission_number").disabled = true;
        document.getElementById("first_name").value = student.first_name;
        document.getElementById("last_name").value = student.last_name;
        document.getElementById("gender").value = student.gender || "";
        document.getElementById("class_name").value = student.class_name;
        document.getElementById("parent_name").value = student.parent_name || "";
        document.getElementById("parent_phone").value = student.parent_phone || "";
        document.getElementById("fee_balance").value = student.fee_balance;

        document.getElementById("student-modal").style.display = "flex";

    } catch (error) {
        alert("Could not load student details: " + error.message);
    }
}


function closeStudentModal() {
    document.getElementById("student-modal").style.display = "none";
    document.getElementById("admission_number").disabled = false;
    editingStudentId = null;
}


async function submitStudentForm(event) {
    event.preventDefault();

    const payload = {
        admission_number: document.getElementById("admission_number").value,
        first_name: document.getElementById("first_name").value,
        last_name: document.getElementById("last_name").value,
        gender: document.getElementById("gender").value || null,
        class_name: document.getElementById("class_name").value,
        parent_name: document.getElementById("parent_name").value || null,
        parent_phone: document.getElementById("parent_phone").value || null,
        fee_balance: parseFloat(document.getElementById("fee_balance").value) || 0
    };

    try {
        let response;

        if (editingStudentId) {
            const { admission_number, ...updatePayload } = payload;
            response = await fetch(apiUrl(`/students/${editingStudentId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(updatePayload)
            });
        } else {
            response = await fetch(apiUrl(`/students`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
        }

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || "Request failed");
        }

        closeStudentModal();
        loadStudents();

    } catch (error) {
        alert("Error saving student: " + error.message);
    }
}


async function deleteStudent(studentId) {

    if (!confirm("Are you sure you want to delete this student?")) return;

    try {
        const response = await fetch(apiUrl(`/students/${studentId}`), {
            method: "DELETE"
        });

        if (!response.ok) {
            throw new Error("Failed to delete student");
        }

        loadStudents();

    } catch (error) {
        alert("Error deleting student: " + error.message);
    }
}


document.addEventListener("DOMContentLoaded", () => {
    if (document.getElementById("students")?.classList.contains("active-page")) {
        loadStudents();
    }
});

// ===================== PAYMENTS =====================

let editingPaymentId = null;   // null while adding, set while editing
let paymentStudents = [];      // cached students, used to resolve names in the table
let allPayments = [];          // last fetched page of payments, used for the stats


// Make the Payments tab load real data whenever it is opened.
// Students MUST be loaded first: the table resolves student_id -> name from the
// cached list, so loading payments in parallel made every row render as
// "Unassigned" even though the payment did have a student.
const _showPageOriginal = showPage;
showPage = function (pageId, clickedItem = null) {
    _showPageOriginal(pageId, clickedItem);
    if (pageId === "payments") {
        loadStudentsForPayments().then(loadPayments);
    }
};


function formatPaymentDate(dateStr) {
    if (!dateStr) return "";
    // Date-only strings must not be parsed as UTC, hence the explicit T00:00:00.
    const d = new Date(dateStr + "T00:00:00");
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}


function formatKsh(value) {
    const num = Number(value || 0);
    return `KSh ${num.toLocaleString("en-KE", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}


function paymentBadge(status) {
    const map = { Matched: "success", Review: "warning", Unmatched: "danger" };
    const cls = map[status] || "";
    return `<span class="badge ${cls}">${escapeHtml(status || "-")}</span>`;
}


function studentNameFor(studentId) {
    const student = paymentStudents.find(s => s.id === studentId);
    return student ? `${student.first_name} ${student.last_name}` : null;
}


/*
 * Pull a human-readable message out of a FastAPI error response.
 * FastAPI returns {"detail": "..."} for HTTPException but a list of field
 * objects for validation errors, so both shapes need handling.
 */
async function readApiError(response, fallback) {
    try {
        const data = await response.json();
        if (typeof data.detail === "string") return data.detail;
        if (Array.isArray(data.detail) && data.detail.length) {
            return data.detail.map(err => `${err.loc?.slice(1).join(".") || "field"}: ${err.msg}`).join("; ");
        }
        return fallback;
    } catch (e) {
        return fallback;
    }
}


function showPaymentError(message) {
    const box = document.getElementById("payment-modal-error");
    box.textContent = message;
    box.style.display = "block";
}


function clearPaymentError() {
    const box = document.getElementById("payment-modal-error");
    box.textContent = "";
    box.style.display = "none";
}


/*
 * Build the query string for GET /payments from the toolbar controls.
 * The backend supports student_id, start_date and end_date; the method and
 * free-text search are filtered in the browser since they are cheap and keep
 * the filtering instant while typing.
 */
function buildPaymentQuery() {
    const params = new URLSearchParams();
    const studentId = document.getElementById("pay-filter-student").value;
    const from = document.getElementById("pay-filter-from").value;
    const to = document.getElementById("pay-filter-to").value;

    if (studentId) params.set("student_id", studentId);
    if (from) params.set("start_date", from);
    if (to) params.set("end_date", to);

    const qs = params.toString();
    return qs ? `?${qs}` : "";
}


function applyPaymentFilters() {
    loadPayments();
}


function resetPaymentFilters() {
    document.getElementById("pay-filter-search").value = "";
    document.getElementById("pay-filter-student").value = "";
    document.getElementById("pay-filter-method").value = "";
    document.getElementById("pay-filter-from").value = "";
    document.getElementById("pay-filter-to").value = "";
    loadPayments();
}


function renderPaymentStats(payments) {
    const total = payments.reduce((sum, p) => sum + Number(p.amount || 0), 0);
    const matched = payments.filter(p => p.status === "Matched");
    const matchedTotal = matched.reduce((sum, p) => sum + Number(p.amount || 0), 0);
    const unassigned = payments.filter(p => !p.student_id).length;

    const dated = payments.filter(p => p.date).map(p => p.date).sort();
    const last = dated.length ? dated[dated.length - 1] : null;

    document.getElementById("pay-stat-total").textContent = formatKsh(total);
    document.getElementById("pay-stat-count").textContent =
        `${payments.length} payment${payments.length === 1 ? "" : "s"}`;

    document.getElementById("pay-stat-matched").textContent = formatKsh(matchedTotal);
    document.getElementById("pay-stat-matched-count").textContent =
        `${matched.length} payment${matched.length === 1 ? "" : "s"}`;

    document.getElementById("pay-stat-unassigned").textContent = unassigned;
    document.getElementById("pay-stat-last").textContent = last ? formatPaymentDate(last) : "-";
}


async function loadStudentsForPayments() {
    const select = document.getElementById("pay-filter-student");
    const current = select.value;
    try {
        const response = await fetch(apiUrl("/students"));
        if (!response.ok) throw new Error("failed");
        paymentStudents = await response.json();

        select.innerHTML = `<option value="">All Students</option>`;
        paymentStudents.forEach(s => {
            const opt = document.createElement("option");
            opt.value = s.id;
            opt.textContent = `${s.first_name} ${s.last_name} (${s.admission_number})`;
            select.appendChild(opt);
        });
        select.value = current;
    } catch (error) {
        console.error("Could not load students for the payment filters:", error);
    }
    return paymentStudents;
}


async function loadPayments() {

    const tbody = document.getElementById("payments-tbody");
    if (!tbody) return;

    tbody.innerHTML = `<tr><td colspan="8">Loading...</td></tr>`;

    try {
        const response = await fetch(apiUrl(`/payments${buildPaymentQuery()}`));
        if (!response.ok) throw new Error(await readApiError(response, "Failed to load payments"));

        let payments = await response.json();

        // Client-side refinements that the API does not filter on.
        const search = document.getElementById("pay-filter-search").value.trim().toLowerCase();
        const method = document.getElementById("pay-filter-method").value;

        if (method) {
            payments = payments.filter(p => p.method === method);
        }
        if (search) {
            payments = payments.filter(p => {
                const name = studentNameFor(p.student_id) || "";
                return [p.transaction_reference, p.payer_name, p.payer_phone, name]
                    .filter(Boolean)
                    .some(field => String(field).toLowerCase().includes(search));
            });
        }

        allPayments = payments;
        renderPaymentStats(payments);

        const note = document.getElementById("pay-results-note");
        note.textContent = `${payments.length} result${payments.length === 1 ? "" : "s"}, newest first`;

        if (payments.length === 0) {
            tbody.innerHTML = `<tr><td colspan="8">No payments found. Click "+ Record Payment" to add one.</td></tr>`;
            return;
        }

        tbody.innerHTML = payments.map(p => {
            const name = studentNameFor(p.student_id);
            return `
            <tr>
                <td>${p.transaction_reference ? escapeHtml(p.transaction_reference) : "-"}</td>
                <td>${name ? escapeHtml(name) : "<em>Unassigned</em>"}</td>
                <td>${escapeHtml(p.method)}</td>
                <td>${p.payer_name ? escapeHtml(p.payer_name) : "-"}</td>
                <td>${formatKsh(p.amount)}</td>
                <td>${formatPaymentDate(p.date)}</td>
                <td>${paymentBadge(p.status)}</td>
                <td>
                    <button class="small-btn" onclick="openEditPaymentModal(${p.id})">Edit</button>
                    <button class="small-btn btn-danger" onclick="deletePayment(${p.id})">Delete</button>
                </td>
            </tr>`;
        }).join("");

    } catch (error) {
        console.error("Error loading payments:", error);
        tbody.innerHTML = `<tr><td colspan="8">${escapeHtml(error.message || "Error loading payments.")}</td></tr>`;
    }
}


async function populatePaymentStudentSelect(selectedId) {
    const select = document.getElementById("pay_student");
    select.innerHTML = `<option value="">-- Unassigned --</option>`;

    try {
        if (paymentStudents.length === 0) {
            const response = await fetch(apiUrl("/students"));
            if (response.ok) paymentStudents = await response.json();
        }
        paymentStudents.forEach(s => {
            const opt = document.createElement("option");
            opt.value = s.id;
            opt.textContent = `${s.first_name} ${s.last_name} (${s.admission_number})`;
            select.appendChild(opt);
        });
    } catch (error) {
        console.error("Could not load students for the payment form:", error);
    }

    if (selectedId) select.value = String(selectedId);
}


function openPaymentModal() {
    editingPaymentId = null;
    clearPaymentError();
    document.getElementById("payment-modal-title").textContent = "Record Payment";
    document.getElementById("payment-submit-btn").textContent = "Save Payment";
    document.getElementById("payment-form").reset();
    document.getElementById("pay_date").value = new Date().toISOString().slice(0, 10);
    populatePaymentStudentSelect(null);
    document.getElementById("payment-modal").style.display = "flex";
}


async function openEditPaymentModal(paymentId) {

    clearPaymentError();

    try {
        const response = await fetch(apiUrl(`/payments/${paymentId}`));
        if (!response.ok) throw new Error(await readApiError(response, "Failed to load payment"));

        const p = await response.json();

        editingPaymentId = p.id;
        document.getElementById("payment-modal-title").textContent = `Edit Payment #${p.id}`;
        document.getElementById("payment-submit-btn").textContent = "Update Payment";

        document.getElementById("pay_amount").value = p.amount;
        document.getElementById("pay_method").value = p.method || "M-Pesa";
        document.getElementById("pay_reference").value = p.transaction_reference || "";
        document.getElementById("pay_payer_name").value = p.payer_name || "";
        document.getElementById("pay_payer_phone").value = p.payer_phone || "";
        document.getElementById("pay_date").value = p.date || "";
        document.getElementById("pay_notes").value = p.notes || "";

        await populatePaymentStudentSelect(p.student_id);

        document.getElementById("payment-modal").style.display = "flex";

    } catch (error) {
        alert("Could not load payment details: " + error.message);
    }
}


function closePaymentModal() {
    document.getElementById("payment-modal").style.display = "none";
    clearPaymentError();
    editingPaymentId = null;
}


async function submitPaymentForm(event) {
    event.preventDefault();
    clearPaymentError();

    const studentValue = document.getElementById("pay_student").value;

    const payload = {
        student_id: studentValue ? parseInt(studentValue, 10) : null,
        amount: document.getElementById("pay_amount").value,
        method: document.getElementById("pay_method").value,
        transaction_reference: document.getElementById("pay_reference").value.trim() || null,
        payer_name: document.getElementById("pay_payer_name").value.trim() || null,
        payer_phone: document.getElementById("pay_payer_phone").value.trim() || null,
        date: document.getElementById("pay_date").value || null,
        notes: document.getElementById("pay_notes").value.trim() || null
    };

    const btn = document.getElementById("payment-submit-btn");
    btn.disabled = true;

    try {
        const response = await fetch(
            editingPaymentId ? apiUrl(`/payments/${editingPaymentId}`) : apiUrl("/payments"),
            {
                method: editingPaymentId ? "PUT" : "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            }
        );

        if (!response.ok) {
            // Show the message inside the modal rather than as an alert, so the
            // user keeps their typed data and can correct it.
            showPaymentError(await readApiError(response, "Could not save this payment."));
            return;
        }

        closePaymentModal();
        loadPayments();

    } catch (error) {
        showPaymentError("Could not reach the server: " + error.message);
    } finally {
        btn.disabled = false;
    }
}


async function deletePayment(paymentId) {

    if (!confirm("Delete this payment? This cannot be undone.")) return;

    try {
        const response = await fetch(apiUrl(`/payments/${paymentId}`), { method: "DELETE" });

        if (!response.ok) {
            throw new Error(await readApiError(response, "Failed to delete payment"));
        }

        loadPayments();

    } catch (error) {
        alert("Error deleting payment: " + error.message);
    }
}


// Re-filter as the user types in the search box without needing to click Filter.
document.addEventListener("DOMContentLoaded", () => {
    const search = document.getElementById("pay-filter-search");
    if (search) search.addEventListener("input", loadPayments);

    // Close the modal on Escape.
    document.addEventListener("keydown", event => {
        if (event.key === "Escape") closePaymentModal();
    });

    if (document.getElementById("payments")?.classList.contains("active-page")) {
        loadStudentsForPayments().then(loadPayments);
    }
});// ===================== REPORTS =====================

function reportPeriod() {
    const year = document.getElementById("report-year")?.value.trim() || "2026";
    const term = document.getElementById("report-term")?.value || "Term 1";
    return { year, term };
}


function showReportError(message) {
    const box = document.getElementById("report-error");
    box.textContent = message;
    box.style.display = "block";
}


function clearReportError() {
    const box = document.getElementById("report-error");
    box.textContent = "";
    box.style.display = "none";
}


function closeReportOutput() {
    document.getElementById("report-output").style.display = "none";
}


function showReport(title, subtitle, html) {
    document.getElementById("report-output-title").textContent = title;
    document.getElementById("report-output-subtitle").textContent = subtitle;
    document.getElementById("report-output-body").innerHTML = html;
    document.getElementById("report-output").style.display = "block";
}


function reportTable(headers, rows) {
    if (!rows.length) {
        return `<p style="color:#64748b; font-size:13px;">No data for this period.</p>`;
    }
    return `
        <div class="table-container">
            <table>
                <thead><tr>${headers.map(h => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead>
                <tbody>
                    ${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}
                </tbody>
            </table>
        </div>`;
}


/*
 * Shared fetch wrapper: turns a failed request into a visible message in the
 * report panel instead of silently doing nothing (which is what the dead
 * buttons used to do).
 */
async function fetchReport(path) {
    clearReportError();
    const response = await fetch(apiUrl(path));
    if (!response.ok) {
        throw new Error(await readApiError(response, `Request failed (${response.status})`));
    }
    return response.json();
}


async function loadCollectionSummary() {
    const { year, term } = reportPeriod();
    try {
        const data = await fetchReport(`/api/v1/reports/collection-summary?academic_year=${encodeURIComponent(year)}&term=${encodeURIComponent(term)}`);

        const rows = data.classes.flatMap(cls =>
            cls.streams.map(s => [
                escapeHtml(cls.grade_level),
                escapeHtml(s.class_name),
                formatKsh(s.expected_amount),
                formatKsh(s.collected_amount),
                formatKsh(s.outstanding_amount),
                `${s.collection_rate_percentage}%`
            ])
        );

        showReport(
            "Fee Collection Report",
            `${term} ${year} - ${data.total_students_enrolled} students, ${data.overall_collection_rate}% collected`,
            `<div class="stats-grid">
                <div class="stat-card"><div><span>Expected</span><h2>${formatKsh(data.total_expected_fees)}</h2></div></div>
                <div class="stat-card"><div><span>Collected</span><h2>${formatKsh(data.total_collected_fees)}</h2></div></div>
                <div class="stat-card"><div><span>Outstanding</span><h2>${formatKsh(data.total_outstanding_arrears)}</h2></div></div>
            </div>` +
            reportTable(["Grade", "Stream", "Expected", "Collected", "Outstanding", "Rate"], rows)
        );
    } catch (error) {
        showReportError("Could not build the Fee Collection Report: " + error.message);
    }
}


async function loadStudentBalances() {
    try {
        const students = await fetchReport("/students");
        const withBalance = students
            .filter(s => Number(s.fee_balance || 0) > 0)
            .sort((a, b) => Number(b.fee_balance) - Number(a.fee_balance));

        const rows = withBalance.map(s => [
            escapeHtml(s.admission_number),
            escapeHtml(`${s.first_name} ${s.last_name}`),
            escapeHtml(s.class_name),
            escapeHtml(s.parent_name || "-"),
            escapeHtml(s.parent_phone || "-"),
            formatKsh(s.fee_balance)
        ]);

        const total = withBalance.reduce((sum, s) => sum + Number(s.fee_balance || 0), 0);

        showReport(
            "Student Balance Report",
            `${withBalance.length} students owing ${formatKsh(total)}`,
            reportTable(["Admission No.", "Student", "Class", "Parent", "Phone", "Balance"], rows)
        );
    } catch (error) {
        showReportError("Could not build the Student Balance Report: " + error.message);
    }
}


async function loadPaymentReport() {
    try {
        const payments = await fetchReport("/payments");
        const rows = payments.map(p => [
            escapeHtml(p.transaction_reference || "-"),
            p.date ? escapeHtml(formatPaymentDate(p.date)) : "-",
            escapeHtml(p.method),
            escapeHtml(p.payer_name || "-"),
            formatKsh(p.amount),
            escapeHtml(p.status || "-")
        ]);
        const total = payments.reduce((sum, p) => sum + Number(p.amount || 0), 0);

        showReport(
            "Payment Report",
            `${payments.length} payments totalling ${formatKsh(total)}`,
            reportTable(["Reference", "Date", "Method", "Payer", "Amount", "Status"], rows)
        );
    } catch (error) {
        showReportError("Could not build the Payment Report: " + error.message);
    }
}


async function loadArrearsReport() {
    const { year, term } = reportPeriod();
    try {
        const data = await fetchReport(`/api/v1/arrears/report?min_balance=1&min_days_overdue=0`);

        const rows = data.students.map(s => [
            escapeHtml(s.admission_number),
            escapeHtml(s.student_name),
            escapeHtml(s.class_name),
            escapeHtml(s.parent_phone || "-"),
            formatKsh(s.total_balance),
            String(s.days_overdue),
            `<span class="badge ${s.urgency_level === "CRITICAL" ? "danger" : s.urgency_level === "MODERATE" ? "warning" : "success"}">${escapeHtml(s.urgency_level)}</span>`
        ]);

        showReport(
            "Arrears Report",
            `${data.total_overdue_students} students owing ${formatKsh(data.total_arrears_amount)}`,
            `<div class="stats-grid">
                <div class="stat-card"><div><span>Critical</span><h2>${data.critical_count}</h2></div></div>
                <div class="stat-card"><div><span>Moderate</span><h2>${data.moderate_count}</h2></div></div>
                <div class="stat-card"><div><span>Mild</span><h2>${data.mild_count}</h2></div></div>
            </div>` +
            reportTable(["Admission No.", "Student", "Class", "Phone", "Balance", "Days Overdue", "Urgency"], rows)
        );
    } catch (error) {
        showReportError("Could not build the Arrears Report: " + error.message);
    }
}


async function loadTrialBalance() {
    const { year, term } = reportPeriod();
    try {
        const data = await fetchReport(`/api/v1/reports/trial-balance?academic_year=${encodeURIComponent(year)}&term=${encodeURIComponent(term)}`);

        const rows = data.line_items.map(li => [
            escapeHtml(li.account_code),
            escapeHtml(li.account_name),
            escapeHtml(li.account_category),
            formatKsh(li.debit),
            formatKsh(li.credit)
        ]);

        showReport(
            "Trial Balance",
            `${data.surplus_or_deficit} - balanced: ${data.is_balanced ? "yes" : "no"}`,
            `<div class="stats-grid">
                <div class="stat-card"><div><span>Total Debits</span><h2>${formatKsh(data.total_debits)}</h2></div></div>
                <div class="stat-card"><div><span>Total Credits</span><h2>${formatKsh(data.total_credits)}</h2></div></div>
                <div class="stat-card"><div><span>Net Result</span><h2>${formatKsh(data.net_operating_result)}</h2></div></div>
            </div>` +
            `<p style="font-size:13px; color:#475569; margin:16px 0;">${escapeHtml(data.executive_commentary)}</p>` +
            reportTable(["Code", "Account", "Category", "Debit", "Credit"], rows)
        );
    } catch (error) {
        showReportError("Could not build the Trial Balance: " + error.message);
    }
}


function downloadAuditTrail() {
    // The backend streams this as a file, so use a hidden anchor rather than fetch().
    clearReportError();
    try {
        const link = document.createElement("a");
        link.href = apiUrl("/api/v1/reports/audit-trail/download");
        link.download = "";
        document.body.appendChild(link);
        link.click();
        link.remove();
    } catch (error) {
        showReportError("Could not start the audit trail download: " + error.message);
    }
}


// Open Reports tab loads nothing by itself; each card fetches on click.
const _showPageForReports = showPage;
showPage = function (pageId, clickedItem = null) {
    _showPageForReports(pageId, clickedItem);
    if (pageId === "reports") {
        clearReportError();
    }
};// ===================== SYSTEM ADMIN =====================

let adminSchools = [];
let adminUsers = [];
let adminStudents = [];
let adminPayments = [];
let editingSchoolId = null;
let editingUserId = null;


// Only platform admins may enter this area. The nav link is hidden for school
// users, and every route is 403-guarded server-side as well.
function isPlatformAdmin() {
    return Boolean(currentUser && currentUser.is_platform_admin);
}


function closeAdminModal(modalId) {
    document.getElementById(modalId).style.display = "none";
}


function adminError(modalErrorId, message) {
    setFormError(modalErrorId, message);
}


async function adminFetch(path, options = {}) {
    const response = await fetch(apiUrl(path), options);
    if (response.status === 403) {
        throw new Error("This area is restricted to the system administrator.");
    }
    if (!response.ok) {
        throw new Error(await readApiError(response, `Request failed (${response.status})`));
    }
    return response.status === 204 ? null : response.json();
}


async function loadAdminSection(section) {
    if (!isPlatformAdmin()) return;

    document.querySelectorAll(".admin-tab").forEach(tab => {
        tab.classList.toggle("active", tab.dataset.section === section);
    });
    ["overview", "schools", "users", "students", "revenue", "payments"].forEach(name => {
        document.getElementById(`admin-section-${name}`).style.display = name === section ? "block" : "none";
    });

    setFormError("admin-error", "");

    try {
        if (section === "overview") await loadAdminOverview();
        if (section === "schools") await loadAdminSchools();
        if (section === "users") await loadAdminUsers();
        if (section === "students") await loadAdminStudents();
        if (section === "revenue") await loadAdminRevenue();
        if (section === "payments") await loadAdminPayments();
    } catch (error) {
        setFormError("admin-error", error.message);
    }
}


// --- Subscription billing / revenue (platform admin) ---

async function loadAdminRevenue() {
    const [revenue, invoices] = await Promise.all([
        adminFetch("/api/subscriptions/revenue"),
        adminFetch("/api/subscriptions/all")
    ]);

    document.getElementById("admin-revenue-stats").innerHTML = `
        <div class="stat-card">
            <div class="stat-icon blue">📈</div>
            <div><span>Monthly recurring</span><h2>${formatKsh(revenue.mrr)}</h2><small>${revenue.active_subscriptions} active</small></div>
        </div>
        <div class="stat-card">
            <div class="stat-icon green">✅</div>
            <div><span>Collected this month</span><h2>${formatKsh(revenue.collected_this_month)}</h2><small>all invoices</small></div>
        </div>
        <div class="stat-card">
            <div class="stat-icon orange">⏳</div>
            <div><span>Outstanding</span><h2>${formatKsh(revenue.outstanding_total)}</h2><small>${revenue.past_due_count} past due</small></div>
        </div>
        <div class="stat-card">
            <div class="stat-icon purple">⚠️</div>
            <div><span>Overdue</span><h2>${formatKsh(revenue.overdue_total)}</h2><small>${revenue.overdue_count} invoice(s)</small></div>
        </div>`;

    const tbody = document.getElementById("admin-invoices-tbody");
    if (!invoices.length) {
        tbody.innerHTML = `<tr><td colspan="8">No subscription invoices yet. Use "Generate this month's invoices".</td></tr>`;
        return;
    }

    adminInvoiceCache = invoices;
    tbody.innerHTML = invoices.map(inv => `
        <tr>
            <td><strong>${escapeHtml(inv.invoice_number)}</strong></td>
            <td>${escapeHtml(inv.school_name || "-")}</td>
            <td>${formatPaymentDate(inv.period_start)} – ${formatPaymentDate(inv.period_end)}</td>
            <td>${formatPaymentDate(inv.due_date)}</td>
            <td>${formatKsh(inv.amount_due)}</td>
            <td>${Number(inv.balance) > 0 ? formatKsh(inv.balance) : "—"}</td>
            <td><span class="plan-pill status-${escapeHtml(inv.status)}">${escapeHtml(inv.status)}</span></td>
            <td>
                ${Number(inv.balance) > 0 && inv.status !== "waived"
                    ? `<button class="small-btn" onclick="recordSubscriptionPayment(${inv.id})">Record payment</button>
                       <button class="small-btn btn-danger" onclick="waiveSubscriptionInvoice(${inv.id})">Waive</button>`
                    : (inv.payments.length
                        ? `<button class="small-btn" onclick="showAdminInvoicePayments(${inv.id})">${inv.payments.length} payment(s)</button>`
                        : "-")}
            </td>
        </tr>`).join("");
}


let adminInvoiceCache = [];


async function generateSubscriptionInvoices() {
    if (!confirm("Generate subscription invoices for every active school?\n\nSafe to run more than once - schools already invoiced this month are skipped.")) return;
    try {
        const created = await adminFetch("/api/subscriptions/generate", { method: "POST" });
        alert(created.length
            ? `Created ${created.length} invoice(s), totalling ${formatKsh(created.reduce((s, i) => s + Number(i.amount_due || 0), 0))}.`
            : "Every active school already has an invoice for this month.");
        loadAdminSection("revenue");
    } catch (error) {
        setFormError("admin-error", error.message);
    }
}


async function recordSubscriptionPayment(invoiceId) {
    const invoice = adminInvoiceCache.find(i => i.id === invoiceId);
    if (!invoice) return;

    const amount = prompt(
        `Record a payment against ${invoice.invoice_number}\n\n` +
        `School: ${invoice.school_name}\n` +
        `Outstanding: KSh ${Number(invoice.balance).toLocaleString()}\n\n` +
        `Amount received (KES):`,
        String(invoice.balance)
    );
    if (amount === null) return;

    const value = Number(amount);
    if (!value || value <= 0) {
        alert("Enter an amount greater than zero.");
        return;
    }

    const method = prompt("Method?\n1 = M-Pesa\n2 = Bank Transfer\n3 = Cheque", "1");
    if (method === null) return;
    const methods = { "1": "M-Pesa", "2": "Bank Transfer", "3": "Cheque" };

    const reference = prompt("Reference / receipt number (optional):", "") ?? "";

    try {
        await adminFetch(`/api/subscriptions/invoices/${invoiceId}/pay`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                amount: value,
                method: methods[String(method).trim()] || "M-Pesa",
                reference: reference.trim() || null
            })
        });
        loadAdminSection("revenue");
    } catch (error) {
        setFormError("admin-error", error.message);
    }
}


async function waiveSubscriptionInvoice(invoiceId) {
    const reason = prompt("Why is this being waived? (goodwill, billing error, etc.)", "billing error");
    if (reason === null) return;
    if (!confirm("Write this invoice off? The school will not be charged for it.")) return;

    try {
        await adminFetch(`/api/subscriptions/invoices/${invoiceId}/waive?reason=${encodeURIComponent(reason)}`, { method: "POST" });
        loadAdminSection("revenue");
    } catch (error) {
        setFormError("admin-error", error.message);
    }
}


function showAdminInvoicePayments(invoiceId) {
    const invoice = adminInvoiceCache.find(i => i.id === invoiceId);
    if (!invoice || !invoice.payments.length) return;

    alert(invoice.payments.map(p =>
        `${p.paid_at ? p.paid_at.slice(0, 10) : "?"}  KSh ${Number(p.amount).toLocaleString()}  ${p.method}  ${p.reference || ""}`
    ).join("\n"));
}


async function populateAdminSchoolSelects() {
    const selects = ["admin_user_school", "admin_student_school", "admin_payment_school", "au_school"];
    const values = selects.map(id => document.getElementById(id)?.value || "");

    for (const id of selects) {
        const el = document.getElementById(id);
        if (!el) continue;
        const isUserSchool = id === "au_school";
        el.innerHTML = isUserSchool
            ? `<option value="">-- No school (platform staff) --</option>`
            : `<option value="">All schools</option>`;
        adminSchools.forEach(s => {
            const opt = document.createElement("option");
            opt.value = s.id;
            opt.textContent = s.name;
            el.appendChild(opt);
        });
    }

    selects.forEach((id, i) => {
        const el = document.getElementById(id);
        if (el && values[i]) el.value = values[i];
    });
}


async function loadAdminOverview() {
    const s = await adminFetch("/api/admin/stats");

    document.getElementById("admin-stats").innerHTML = `
        <div class="stat-card">
            <div class="stat-icon blue">🏫</div>
            <div><span>Schools</span><h2>${s.total_schools}</h2><small>${s.active_subscriptions} subscribed</small></div>
        </div>
        <div class="stat-card">
            <div class="stat-icon green">🎓</div>
            <div><span>Students</span><h2>${s.total_students}</h2><small>across all schools</small></div>
        </div>
        <div class="stat-card">
            <div class="stat-icon purple">💳</div>
            <div><span>Payments</span><h2>${s.total_payments}</h2><small>${formatKsh(s.total_payments_value)}</small></div>
        </div>
        <div class="stat-card">
            <div class="stat-icon orange">⚠️</div>
            <div><span>At capacity</span><h2>${s.schools_at_capacity}</h2><small>${s.cancelled} cancelled</small></div>
        </div>`;

    const plans = Object.entries(s.all_plans || {});
    document.getElementById("admin-plans").innerHTML = plans.length
        ? `<div class="table-container"><table>
            <thead><tr><th>Plan</th><th>Schools</th></tr></thead>
            <tbody>${plans.map(([plan, count]) => `
                <tr><td><span class="plan-pill plan-${escapeHtml(plan)}">${escapeHtml(plan)}</span></td><td>${count}</td></tr>
            `).join("")}</tbody></table></div>`
        : `<p style="color:#64748b;font-size:13px;">No schools registered yet.</p>`;
}


async function loadAdminSchools() {
    const params = new URLSearchParams();
    const search = document.getElementById("admin_school_search").value.trim();
    const plan = document.getElementById("admin_school_plan").value;
    const status = document.getElementById("admin_school_status").value;
    if (search) params.set("search", search);
    if (plan) params.set("plan", plan);
    if (status) params.set("status", status);

    adminSchools = await adminFetch(`/api/admin/schools?${params}`);
    await populateAdminSchoolSelects();

    const tbody = document.getElementById("admin-schools-tbody");
    if (!adminSchools.length) {
        tbody.innerHTML = `<tr><td colspan="7">No schools match this filter.</td></tr>`;
        return;
    }

    tbody.innerHTML = adminSchools.map(s => {
        const cap = s.max_students ? Math.min(100, (s.student_count / s.max_students) * 100) : 0;
        const full = s.at_capacity;
        const limit = s.max_students ?? "∞";
        return `
        <tr>
            <td><strong>${escapeHtml(s.name)}</strong></td>
            <td>${escapeHtml(s.code)}</td>
            <td><span class="plan-pill plan-${escapeHtml(s.plan)}">${escapeHtml(s.plan)}</span></td>
            <td><span class="plan-pill status-${escapeHtml(s.subscription_status)}">${escapeHtml(s.subscription_status)}</span></td>
            <td>
                ${s.student_count}/${limit}
                <div class="capacity-bar ${full ? "full" : ""}"><span style="width:${cap}%"></span></div>
            </td>
            <td>${escapeHtml(s.email || "-")}</td>
            <td>
                <button class="small-btn" onclick="openEditAdminSchool(${s.id})">Edit</button>
                <button class="small-btn btn-danger" onclick="deleteAdminSchool(${s.id}, '${escapeHtml(s.name)}')">Delete</button>
            </td>
        </tr>`;
    }).join("");
}


async function loadAdminUsers() {
    const params = new URLSearchParams();
    const search = document.getElementById("admin_user_search").value.trim();
    const school = document.getElementById("admin_user_school").value;
    if (search) params.set("search", search);
    if (school) params.set("school_id", school);

    adminUsers = await adminFetch(`/api/admin/users?${params}`);
    await populateAdminSchoolSelects();

    const tbody = document.getElementById("admin-users-tbody");
    if (!adminUsers.length) {
        tbody.innerHTML = `<tr><td colspan="7">No users match this filter.</td></tr>`;
        return;
    }

    tbody.innerHTML = adminUsers.map(u => `
        <tr>
            <td>${escapeHtml(u.email)}</td>
            <td>${escapeHtml(u.full_name || "-")}</td>
            <td>${escapeHtml(u.school_name || "<em>Platform</em>")}</td>
            <td><span class="role-pill role-${u.is_platform_admin ? "platform" : escapeHtml(u.role)}">${escapeHtml(u.role)}</span></td>
            <td>${u.is_platform_admin ? `<span class="role-pill role-platform">SYSTEM ADMIN</span>` : "School user"}</td>
            <td>${u.is_active ? `<span class="badge success">Active</span>` : `<span class="badge danger">Disabled</span>`}</td>
            <td>
                <button class="small-btn" onclick="openEditAdminUser(${u.id})">Edit</button>
                <button class="small-btn btn-danger" onclick="deleteAdminUser(${u.id}, '${escapeHtml(u.email)}')">Delete</button>
            </td>
        </tr>`).join("");
}


async function loadAdminStudents() {
    const params = new URLSearchParams();
    const search = document.getElementById("admin_student_search").value.trim();
    const school = document.getElementById("admin_student_school").value;
    if (search) params.set("search", search);
    if (school) params.set("school_id", school);

    adminStudents = await adminFetch(`/api/admin/students?${params}`);
    await populateAdminSchoolSelects();

    const tbody = document.getElementById("admin-students-tbody");
    if (!adminStudents.length) {
        tbody.innerHTML = `<tr><td colspan="7">No students match this filter.</td></tr>`;
        return;
    }

    tbody.innerHTML = adminStudents.map(s => `
        <tr>
            <td>
                <div class="student">
                    ${s.photo_url
                        ? `<img class="student-avatar" src="${escapeHtml(s.photo_url)}" alt="" style="object-fit:cover;">`
                        : `<div class="student-avatar">${escapeHtml((s.first_name?.[0] || "") + (s.last_name?.[0] || ""))}</div>`}
                    <strong>${escapeHtml(s.first_name)} ${escapeHtml(s.last_name)}</strong>
                </div>
            </td>
            <td>${escapeHtml(s.admission_number)}</td>
            <td>${escapeHtml(s.school_name || "-")}</td>
            <td>${escapeHtml(s.class_name)}</td>
            <td>${escapeHtml(s.parent_name || "-")}</td>
            <td>${formatKsh(s.fee_balance)}</td>
            <td><button class="small-btn btn-danger" onclick="deleteAdminStudent(${s.id}, '${escapeHtml(s.first_name)} ${escapeHtml(s.last_name)}')">Delete</button></td>
        </tr>`).join("");
}


async function loadAdminPayments() {
    const params = new URLSearchParams();
    const search = document.getElementById("admin_payment_search").value.trim();
    const school = document.getElementById("admin_payment_school").value;
    if (search) params.set("search", search);
    if (school) params.set("school_id", school);

    adminPayments = await adminFetch(`/api/admin/payments?${params}`);
    await populateAdminSchoolSelects();

    const tbody = document.getElementById("admin-payments-tbody");
    if (!adminPayments.length) {
        tbody.innerHTML = `<tr><td colspan="7">No payments match this filter.</td></tr>`;
        return;
    }

    tbody.innerHTML = adminPayments.map(p => `
        <tr>
            <td>${escapeHtml(p.transaction_reference || "-")}</td>
            <td>${escapeHtml(p.school_name || "-")}</td>
            <td>${escapeHtml(p.payer_name || "-")}</td>
            <td>${escapeHtml(p.method || "-")}</td>
            <td>${formatKsh(p.amount)}</td>
            <td>${formatPaymentDate(p.date)}</td>
            <td><button class="small-btn btn-danger" onclick="deleteAdminPayment(${p.id})">Delete</button></td>
        </tr>`).join("");
}


// --- Schools CRUD ---

function openAdminSchoolModal() {
    editingSchoolId = null;
    adminError("admin-school-error", "");
    document.getElementById("admin-school-form").reset();
    document.getElementById("admin-school-modal-title").textContent = "New School";
    // Admin credentials only make sense when creating a school.
    document.getElementById("as_admin_fields").style.display = "block";
    document.getElementById("as_status_wrap").style.display = "none";
    document.getElementById("admin-school-modal").style.display = "flex";
}


function openEditAdminSchool(schoolId) {
    const school = adminSchools.find(s => s.id === schoolId);
    if (!school) return;

    editingSchoolId = schoolId;
    adminError("admin-school-error", "");
    document.getElementById("admin-school-modal-title").textContent = `Edit ${school.name}`;
    document.getElementById("as_name").value = school.name;
    document.getElementById("as_code").value = school.code;
    document.getElementById("as_email").value = school.email || "";
    document.getElementById("as_phone").value = school.phone || "";
    document.getElementById("as_plan").value = school.plan;
    document.getElementById("as_status").value = school.subscription_status;

    document.getElementById("as_admin_fields").style.display = "none";
    document.getElementById("as_status_wrap").style.display = "block";
    document.getElementById("admin-school-modal").style.display = "flex";
}


async function submitAdminSchool(event) {
    event.preventDefault();
    adminError("admin-school-error", "");

    const body = {
        name: document.getElementById("as_name").value.trim(),
        code: document.getElementById("as_code").value.trim().toUpperCase(),
        email: document.getElementById("as_email").value.trim() || null,
        phone: document.getElementById("as_phone").value.trim() || null,
        plan: document.getElementById("as_plan").value
    };

    try {
        if (editingSchoolId) {
            body.subscription_status = document.getElementById("as_status").value;
            await adminFetch(`/api/admin/schools/${editingSchoolId}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            });
        } else {
            body.admin_name = document.getElementById("as_admin_name").value.trim() || null;
            body.admin_email = document.getElementById("as_admin_email").value.trim();
            body.admin_password = document.getElementById("as_admin_password").value;
            await adminFetch("/api/admin/schools", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            });
        }

        closeAdminModal("admin-school-modal");
        loadAdminSection("schools");
    } catch (error) {
        adminError("admin-school-error", error.message);
    }
}


async function deleteAdminSchool(schoolId, name) {
    if (!confirm(`Delete "${name}" and ALL of its students, payments and users?\n\nThis cannot be undone.`)) return;
    try {
        const result = await adminFetch(`/api/admin/schools/${schoolId}`, { method: "DELETE" });
        alert(result.message);
        loadAdminSection("schools");
    } catch (error) {
        setFormError("admin-error", error.message);
    }
}


// --- Users CRUD ---

function openAdminUserModal() {
    editingUserId = null;
    adminError("admin-user-error", "");
    document.getElementById("admin-user-form").reset();
    document.getElementById("admin-user-modal-title").textContent = "New User";
    document.getElementById("au_password").required = true;
    document.getElementById("au_school").style.display = "block";
    document.getElementById("au_platform").closest("label").style.display = "flex";
    document.getElementById("admin-user-modal").style.display = "flex";
}


function openEditAdminUser(userId) {
    const user = adminUsers.find(u => u.id === userId);
    if (!user) return;

    editingUserId = userId;
    adminError("admin-user-error", "");
    document.getElementById("admin-user-modal-title").textContent = `Edit ${user.email}`;
    document.getElementById("au_email").value = user.email;
    document.getElementById("au_email").disabled = true;
    document.getElementById("au_name").value = user.full_name || "";
    document.getElementById("au_school").value = user.school_id || "";
    document.getElementById("au_role").value = user.role;
    document.getElementById("au_password").value = "";
    document.getElementById("au_password").required = false;
    document.getElementById("au_platform").checked = user.is_platform_admin;
    document.getElementById("au_platform").closest("label").style.display = "none";
    document.getElementById("admin-user-modal").style.display = "flex";
}


async function submitAdminUser(event) {
    event.preventDefault();
    adminError("admin-user-error", "");

    const password = document.getElementById("au_password").value;
    const schoolValue = document.getElementById("au_school").value;

    try {
        if (editingUserId) {
            const body = {
                full_name: document.getElementById("au_name").value.trim() || null,
                school_id: schoolValue ? parseInt(schoolValue, 10) : null,
                role: document.getElementById("au_role").value
            };
            if (password) body.password = password;
            await adminFetch(`/api/admin/users/${editingUserId}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            });
        } else {
            const body = {
                email: document.getElementById("au_email").value.trim(),
                full_name: document.getElementById("au_name").value.trim() || null,
                school_id: schoolValue ? parseInt(schoolValue, 10) : null,
                role: document.getElementById("au_role").value,
                password,
                is_platform_admin: document.getElementById("au_platform").checked
            };
            await adminFetch("/api/admin/users", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            });
        }

        closeAdminModal("admin-user-modal");
        loadAdminSection("users");
    } catch (error) {
        adminError("admin-user-error", error.message);
    }
}


async function deleteAdminUser(userId, email) {
    if (!confirm(`Delete user "${email}"?`)) return;
    try {
        await adminFetch(`/api/admin/users/${userId}`, { method: "DELETE" });
        loadAdminSection("users");
    } catch (error) {
        setFormError("admin-error", error.message);
    }
}


// --- Students / payments delete (cross-tenant) ---

async function deleteAdminStudent(studentId, name) {
    if (!confirm(`Delete student "${name}" and their payment history?`)) return;
    try {
        await adminFetch(`/api/admin/students/${studentId}`, { method: "DELETE" });
        loadAdminSection("students");
    } catch (error) {
        setFormError("admin-error", error.message);
    }
}


async function deleteAdminPayment(paymentId) {
    if (!confirm("Delete this payment? This cannot be undone.")) return;
    try {
        await adminFetch(`/api/admin/payments/${paymentId}`, { method: "DELETE" });
        loadAdminSection("payments");
    } catch (error) {
        setFormError("admin-error", error.message);
    }
}


// Load the schools list so the filter dropdowns have something to show.
const _showPageForAdmin = showPage;
showPage = function (pageId, clickedItem = null) {
    _showPageForAdmin(pageId, clickedItem);
    if (pageId === "admin" && isPlatformAdmin()) {
        adminFetch("/api/admin/schools")
            .then(schools => { adminSchools = schools; populateAdminSchoolSelects(); })
            .catch(error => setFormError("admin-error", error.message))
            .finally(() => loadAdminSection("overview"));
    }
};// ===================== TIMETABLE =====================

const TT_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];

let ttSubjects = [];
let ttClasses = [];
let ttSlots = [];
let ttGrid = null;
let ttTeachers = [];
let ttCell = null;   // { class_id, slot_id, day, entry }


async function ttFetch(path, options = {}) {
    const response = await fetch(apiUrl(path), options);
    if (!response.ok) {
        throw new Error(await readApiError(response, `Request failed (${response.status})`));
    }
    return response.json();
}


async function loadTimetable() {
    try {
        const [classes, subjects] = await Promise.all([
            ttFetch("/api/timetable/classes"),
            ttFetch("/api/timetable/subjects")
        ]);
        ttClasses = classes;
        ttSubjects = subjects;

        const select = document.getElementById("tt-class");
        const current = select.value;
        select.innerHTML = classes.length
            ? ""
            : `<option value="">No classes yet - set up subjects and classes first</option>`;
        classes.forEach(c => {
            const opt = document.createElement("option");
            opt.value = c.id;
            opt.textContent = `${c.name} (${c.student_count} students)`;
            select.appendChild(opt);
        });
        if (current && classes.some(c => String(c.id) === current)) select.value = current;

        await loadTimetableGrid();
    } catch (error) {
        document.getElementById("tt-conflicts").innerHTML =
            `<div class="modal-error">${escapeHtml(error.message)}</div>`;
    }
}


async function loadTimetableGrid() {
    const classId = document.getElementById("tt-class").value;
    if (!classId) {
        document.getElementById("tt-head").innerHTML = "";
        document.getElementById("tt-body").innerHTML = "";
        return;
    }

    try {
        ttGrid = await ttFetch(`/api/timetable/classes/${classId}/grid`);
        ttSlots = ttGrid.slots;
        renderTimetableGrid();
    } catch (error) {
        document.getElementById("tt-conflicts").innerHTML =
            `<div class="modal-error">${escapeHtml(error.message)}</div>`;
    }
}


function renderTimetableGrid() {
    if (!ttGrid) return;

    // Index entries by "day|slotId" so the grid is a straight lookup.
    const byCell = {};
    ttGrid.entries.forEach(e => {
        byCell[`${e.day}|${e.slot_id}`] = e;
    });

    document.getElementById("tt-head").innerHTML =
        `<th class="tt-period-col">Period</th>` +
        ttGrid.days.map(d => `<th>${escapeHtml(d)}</th>`).join("");

    const rows = ttSlots.map(slot => {
        // Breaks span the whole row rather than repeating five times.
        if (!slot.is_teaching) {
            return `<tr class="tt-break"><td colspan="${ttGrid.days.length + 1}">${escapeHtml(slot.name)} · ${escapeHtml(slot.start_time)}-${escapeHtml(slot.end_time)}</td></tr>`;
        }

        const cells = ttGrid.days.map(day => {
            const key = `${day}|${slot.id}`;
            const entry = byCell[key];

            if (!entry) {
                return `<td class="tt-cell empty" onclick="openTimetableCell(${slot.id}, '${day}', null)">+</td>`;
            }
            const unassigned = !entry.teacher_name;
            return `<td class="tt-cell" onclick="openTimetableCell(${slot.id}, '${day}', ${entry.id})">
                <div class="tt-lesson ${unassigned ? "tt-unassigned" : ""}">
                    <strong>${escapeHtml(entry.subject_name || "?")}</strong>
                    <small>${entry.teacher_name ? escapeHtml(entry.teacher_name) : "<i>no teacher</i>"}${entry.room ? " · " + escapeHtml(entry.room) : ""}</small>
                </div>
            </td>`;
        }).join("");

        return `
            <tr>
                <td class="tt-period">${escapeHtml(slot.name)}<small>${escapeHtml(slot.start_time)}-${escapeHtml(slot.end_time)}</small></td>
                ${cells}
            </tr>`;
    }).join("");

    document.getElementById("tt-body").innerHTML = rows;
}


async function openTimetableCell(slotId, day, entryId) {
    ttCell = { class_id: Number(document.getElementById("tt-class").value), slot_id: slotId, day, entry_id: entryId };

    const slot = ttSlots.find(s => s.id === slotId);
    const className = ttClasses.find(c => c.id === ttCell.class_id)?.name || "";
    document.getElementById("tt-cell-title").textContent = entryId ? "Edit Lesson" : "Add Lesson";
    document.getElementById("tt-cell-subtitle").textContent =
        `${className} · ${day} · ${slot ? slot.name + " (" + slot.start_time + ")" : ""}`;

    setFormError("tt-cell-error", "");

    const subjectSelect = document.getElementById("tt-cell-subject");
    subjectSelect.innerHTML = `<option value="">-- Select subject --</option>`;
    ttSubjects.forEach(s => {
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.textContent = s.name;
        subjectSelect.appendChild(opt);
    });

    // Teachers = any active user at THIS school. Uses the timetable's own
    // scoped endpoint; the platform-admin users endpoint is 403 for staff.
    try {
        ttTeachers = await ttFetch("/api/timetable/teachers");
    } catch (error) {
        ttTeachers = [];
    }
    const teacherSelect = document.getElementById("tt-cell-teacher");
    teacherSelect.innerHTML = `<option value="">-- No teacher yet --</option>`;
    ttTeachers.forEach(u => {
        const opt = document.createElement("option");
        opt.value = u.id;
        opt.textContent = u.name;
        teacherSelect.appendChild(opt);
    });

    const entry = ttGrid?.entries.find(e => e.id === entryId);
    if (entry) {
        subjectSelect.value = entry.subject_id;
        teacherSelect.value = entry.teacher_id || "";
        document.getElementById("tt-cell-room").value = entry.room || "";
        document.getElementById("tt-cell-note").value = entry.note || "";
    } else {
        subjectSelect.value = "";
        teacherSelect.value = "";
        document.getElementById("tt-cell-room").value = "";
        document.getElementById("tt-cell-note").value = "";
    }

    document.getElementById("tt-cell-delete").style.display = entryId ? "inline-block" : "none";
    document.getElementById("tt-cell-modal").style.display = "flex";
}


function closeTimetableCell() {
    document.getElementById("tt-cell-modal").style.display = "none";
    ttCell = null;
}


async function saveTimetableCell() {
    if (!ttCell) return;
    setFormError("tt-cell-error", "");

    const subjectId = document.getElementById("tt-cell-subject").value;
    if (!subjectId) {
        setFormError("tt-cell-error", "Choose a subject for this lesson.");
        return;
    }

    const teacherValue = document.getElementById("tt-cell-teacher").value;
    const body = {
        class_id: ttCell.class_id,
        slot_id: ttCell.slot_id,
        day: ttCell.day,
        subject_id: parseInt(subjectId, 10),
        teacher_id: teacherValue ? parseInt(teacherValue, 10) : null,
        room: document.getElementById("tt-cell-room").value.trim() || null,
        note: document.getElementById("tt-cell-note").value.trim() || null
    };

    try {
        if (ttCell.entry_id) {
            await ttFetch(`/api/timetable/entries/${ttCell.entry_id}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            });
        } else {
            await ttFetch("/api/timetable/entries", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            });
        }
        closeTimetableCell();
        loadTimetableGrid();
    } catch (error) {
        // The 409s here are the conflict rules - they are the point.
        setFormError("tt-cell-error", error.message);
    }
}


async function deleteTimetableCell() {
    if (!ttCell || !ttCell.entry_id) return;
    try {
        await ttFetch(`/api/timetable/entries/${ttCell.entry_id}`, { method: "DELETE" });
        closeTimetableCell();
        loadTimetableGrid();
    } catch (error) {
        setFormError("tt-cell-error", error.message);
    }
}


async function checkTimetableConflicts() {
    try {
        const report = await ttFetch("/api/timetable/conflicts");
        const box = document.getElementById("tt-conflicts");
        if (!report.count) {
            box.innerHTML = `<div style="background:#dcfce7;border-left:3px solid #16a34a;padding:12px;border-radius:6px;font-size:13px;color:#15803d;margin-bottom:18px;">✓ No conflicts. This timetable is ready to publish.</div>`;
            return;
        }
        box.innerHTML = `<div class="modal-error" style="background:#fef3c7;border-left-color:#d97706;color:#92400e;">
            <strong>${report.count} issue(s) found</strong>
            ${report.conflicts.map(c => `<div style="margin-top:6px;">[${escapeHtml(c.severity)}] ${escapeHtml(c.message)}</div>`).join("")}
        </div>`;
    } catch (error) {
        setFormError("tt-cell-error", error.message);
    }
}


async function autoGenerateTimetable() {
    if (!ttClasses.length) {
        alert("Add some classes first, in Subjects & Classes.");
        return;
    }
    if (!ttSubjects.length) {
        alert("Add some subjects first, in Subjects & Classes.");
        return;
    }
    if (!confirm(`Generate a timetable for all ${ttClasses.length} class(es)?\n\nThis replaces any lessons already scheduled.`)) return;

    try {
        const result = await ttFetch("/api/timetable/auto-generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ class_ids: ttClasses.map(c => c.id), daily_lessons: 8 })
        });
        alert(result.message + "\n\n" + result.note);
        loadTimetable();
    } catch (error) {
        alert("Could not generate: " + error.message);
    }
}


async function clearTimetableForClass() {
    const classId = document.getElementById("tt-class").value;
    if (!classId) return;
    const name = ttClasses.find(c => String(c.id) === classId)?.name || "this class";
    if (!confirm(`Remove every lesson from ${name}?`)) return;

    try {
        const result = await ttFetch(`/api/timetable/classes/${classId}/entries`, { method: "DELETE" });
        alert(result.message);
        loadTimetableGrid();
    } catch (error) {
        alert("Could not clear: " + error.message);
    }
}


function printTimetable() {
    window.print();
}


// --- Setup page ---

async function loadTimetableSetup() {
    document.getElementById("tt-slots-list").innerHTML = "Loading...";
    await loadTimetable();
    renderSetupLists();
    try {
        const slots = await ttFetch("/api/timetable/slots");
        ttSlots = slots;
        document.getElementById("tt-slots-list").innerHTML = slots.map(s => `
            <div class="tt-list-row">
                <div>
                    <strong>${escapeHtml(s.name)}</strong>
                    <small>${escapeHtml(s.start_time)}-${escapeHtml(s.end_time)}</small>
                </div>
                <div style="display:flex;gap:8px;align-items:center;">
                    <span class="badge ${s.is_teaching ? "success" : "warning"}">${s.is_teaching ? "Teaching" : "Break"}</span>
                    <button class="small-btn btn-danger" onclick="deleteTimetableSlot(${s.id})">Delete</button>
                </div>
            </div>`).join("");
    } catch (error) {
        document.getElementById("tt-slots-list").innerHTML = escapeHtml(error.message);
    }
}


function renderSetupLists() {
    document.getElementById("tt-subjects-list").innerHTML = ttSubjects.length
        ? ttSubjects.map(s => `
            <div class="tt-list-row">
                <span><strong>${escapeHtml(s.name)}</strong> ${s.code ? `<small>${escapeHtml(s.code)}</small>` : ""}</span>
                <button class="small-btn btn-danger" onclick="deleteSubject(${s.id}, '${escapeHtml(s.name)}')">Delete</button>
            </div>`).join("")
        : `<p style="color:#94a3b8;font-size:12px;">No subjects yet.</p>`;

    document.getElementById("tt-classes-list").innerHTML = ttClasses.length
        ? ttClasses.map(c => `
            <div class="tt-list-row">
                <span><strong>${escapeHtml(c.name)}</strong> <small>${c.student_count} student(s)</small></span>
                <button class="small-btn btn-danger" onclick="deleteTimetableClass(${c.id}, '${escapeHtml(c.name)}')">Delete</button>
            </div>`).join("")
        : `<p style="color:#94a3b8;font-size:12px;">No classes yet.</p>`;
}


async function addSubject() {
    const name = document.getElementById("tt-subject-name").value.trim();
    if (!name) return;
    try {
        await ttFetch("/api/timetable/subjects", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, code: document.getElementById("tt-subject-code").value.trim() || null })
        });
        document.getElementById("tt-subject-name").value = "";
        document.getElementById("tt-subject-code").value = "";
        await loadTimetableSetup();
    } catch (error) {
        alert(error.message);
    }
}


async function deleteSubject(id, name) {
    if (!confirm(`Delete subject "${name}"?`)) return;
    try {
        await ttFetch(`/api/timetable/subjects/${id}`, { method: "DELETE" });
        await loadTimetableSetup();
    } catch (error) {
        alert(error.message);
    }
}


async function addTimetableClass() {
    const name = document.getElementById("tt-class-name").value.trim();
    if (!name) return;
    try {
        await ttFetch("/api/timetable/classes", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name })
        });
        document.getElementById("tt-class-name").value = "";
        await loadTimetableSetup();
    } catch (error) {
        alert(error.message);
    }
}


async function deleteTimetableClass(id, name) {
    if (!confirm(`Delete class "${name}" and its timetable?`)) return;
    try {
        await ttFetch(`/api/timetable/classes/${id}`, { method: "DELETE" });
        await loadTimetableSetup();
    } catch (error) {
        alert(error.message);
    }
}


async function seedClassesFromStudents() {
    try {
        const classes = await ttFetch("/api/timetable/classes/seed", { method: "POST" });
        alert(`Imported ${classes.length} class(es) from your student list.`);
        await loadTimetableSetup();
    } catch (error) {
        alert(error.message);
    }
}


async function deleteTimetableSlot(id) {
    if (!confirm("Delete this period and any lessons in it?")) return;
    try {
        await ttFetch(`/api/timetable/slots/${id}`, { method: "DELETE" });
        await loadTimetableSetup();
    } catch (error) {
        alert(error.message);
    }
}


// Load data when either timetable view is opened.
const _showPageForTimetable = showPage;
showPage = function (pageId, clickedItem = null) {
    _showPageForTimetable(pageId, clickedItem);
    if (pageId === "timetable") {
        loadTimetable();
    }
    if (pageId === "timetable-setup") {
        loadTimetableSetup();
    }
};// ===================== M-PESA =====================

let mpesaStudents = [];


async function loadMpesaPage() {
    setFormError("mpesa-error", "");
    document.getElementById("mpesa-success").style.display = "none";

    // Status banner - tells the bursar whether Daraja is usable at all.
    try {
        const status = await apiFetch("/api/mpesa/status");
        const banner = document.getElementById("mpesa-status-banner");
        banner.innerHTML = status.configured
            ? `<div style="background:#dcfce7;border-left:3px solid #16a34a;padding:12px;border-radius:6px;font-size:13px;color:#15803d;margin-bottom:18px;">
                   ✓ M-Pesa connected — ${escapeHtml(status.environment)} environment, paybill ${escapeHtml(status.shortcode || "")}
               </div>`
            : `<div style="background:#fef3c7;border-left:3px solid #d97706;padding:12px;border-radius:6px;font-size:13px;color:#92400e;margin-bottom:18px;">
                   <strong>M-Pesa is not configured.</strong> ${escapeHtml(status.message)}
                   <br><small>You can still use <em>Reconcile the till statement</em> — that works without Daraja.</small>
               </div>`;
    } catch (error) {
        document.getElementById("mpesa-status-banner").innerHTML =
            `<div class="modal-error" style="margin-bottom:18px;">${escapeHtml(error.message)}</div>`;
    }

    // Student selector.
    const select = document.getElementById("mp-student");
    const current = select.value;
    try {
        mpesaStudents = await apiFetch("/students");
        select.innerHTML = `<option value="">-- Select a student --</option>`;
        mpesaStudents.forEach(s => {
            const opt = document.createElement("option");
            opt.value = s.id;
            opt.textContent = `${s.first_name} ${s.last_name} (${s.admission_number})`;
            select.appendChild(opt);
        });
        if (current) select.value = current;
    } catch (error) {
        // leave the selector empty; the bursar can still type a phone
    }

    loadMpesaStats();
}


async function apiFetch(path, options = {}) {
    const response = await fetch(apiUrl(path), options);
    if (!response.ok) {
        throw new Error(await readApiError(response, `Request failed (${response.status})`));
    }
    return response.json();
}


async function loadMpesaStats() {
    try {
        const payments = await apiFetch("/payments");
        const today = new Date().toISOString().slice(0, 10);
        const todays = payments.filter(p => p.date === today);
        const received = payments.filter(p => p.method === "M-Pesa");

        document.getElementById("mp-stat-sent").textContent = todays.length;
        document.getElementById("mp-stat-received").textContent =
            formatKsh(received.reduce((sum, p) => sum + Number(p.amount || 0), 0));

        // Students who still owe money - the number a bursar is judged on.
        const mpesaUnmatched = mpesaStudents.filter(s => Number(s.fee_balance || 0) > 0).length;
        document.getElementById("mp-stat-unmatched").textContent = mpesaUnmatched;
    } catch (error) {
        // stats are cosmetic; ignore failures
    }
}


// Selecting a student fills their phone and outstanding balance.
function onMpesaStudentChange() {
    const student = mpesaStudents.find(s => String(s.id) === document.getElementById("mp-student").value);
    if (!student) return;
    if (student.parent_phone) document.getElementById("mp-phone").value = student.parent_phone;
    const balance = Number(student.fee_balance || 0);
    document.getElementById("mp-amount").value = balance > 0 ? Math.round(balance) : "";
    document.getElementById("mp-amount").placeholder = balance > 0
        ? `Outstanding ${formatKsh(balance)}`
        : "5000";
}


async function sendStkPush() {
    setFormError("mpesa-error", "");
    document.getElementById("mpesa-success").style.display = "none";

    const studentValue = document.getElementById("mp-student").value;
    const phone = document.getElementById("mp-phone").value.trim();
    const amount = document.getElementById("mp-amount").value;

    if (!phone) {
        setFormError("mpesa-error", "Enter the parent's phone number.");
        return;
    }
    if (!amount || Number(amount) <= 0) {
        setFormError("mpesa-error", "Enter an amount greater than zero.");
        return;
    }

    const btn = document.getElementById("mp-send-btn");
    btn.disabled = true;
    btn.textContent = "Sending...";

    try {
        const result = await apiFetch("/api/mpesa/stkpush", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                phone_number: phone,
                amount: Number(amount),
                student_id: studentValue ? Number(studentValue) : null
            })
        });

        const box = document.getElementById("mpesa-success");
        box.innerHTML = `✓ ${escapeHtml(result.message)}<br><small style="color:#166534;">Reference ${escapeHtml(result.checkout_request_id)}</small>`;
        box.style.display = "block";
        loadMpesaStats();
    } catch (error) {
        setFormError("mpesa-error", error.message);
    } finally {
        btn.disabled = false;
        btn.textContent = "Send payment request";
    }
}


function downloadStatementTemplate() {
    const csv = "receipt,amount,payer_name,phone,date\n"
        + "QJG7X2K9LM,5000,Paul Kiprotich,0722000001,2026-10-04\n"
        + "MPX-34-99,2500,Fatuma Achieng,0722000002,2026-10-04\n";
    const blob = new Blob([csv], { type: "text/csv" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "mpesa_statement_template.csv";
    link.click();
    URL.revokeObjectURL(link.href);
    alert("Safaricom's own export uses different headings.\n\nUpload it as-is — the matcher recognises:\n  receipt / Receipt Number / Transaction Reference\n  amount / Amount\n  payer_name / Name / Payer\n  phone / Phone\n  date / Date");
}


async function runReconciliation(postMatched) {
    const file = document.getElementById("rec-file").files[0];
    const result = document.getElementById("rec-result");

    if (!file) {
        result.innerHTML = `<div class="modal-error">Choose a statement file first.</div>`;
        return;
    }

    if (postMatched && !confirm("Post the matched payments to student accounts?\n\nThis moves money into the ledger. Already-recorded references are skipped automatically.")) {
        return;
    }

    const btn = postMatched
        ? document.getElementById("rec-post-btn")
        : document.getElementById("rec-run-btn");
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Matching...";
    result.innerHTML = `<p style="font-size:13px;color:#64748b;">Reading statement and matching against students...</p>`;

    try {
        const form = new FormData();
        form.append("file", file);
        form.append("post_matched", postMatched ? "true" : "false");

        const summary = await apiFetch("/api/mpesa/reconcile-file", { method: "POST", body: form });
        renderReconciliation(summary, postMatched);
        loadMpesaStats();
    } catch (error) {
        result.innerHTML = `<div class="modal-error">${escapeHtml(error.message)}</div>`;
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}


function renderReconciliation(summary, postMatched) {
    const headers = ["Receipt", "Amount", "Payer", "Matched to", "Result"];
    const rows = summary.rows.map(r => `
        <tr>
            <td>${escapeHtml(r.receipt || "-")}</td>
            <td>${escapeHtml(r.amount || "-")}</td>
            <td>${escapeHtml(r.payer_name || "-")}</td>
            <td>${r.student_name ? escapeHtml(r.student_name) : "<em>not matched</em>"}</td>
            <td>
                ${r.match_status === "matched"
                    ? (r.posted
                        ? `<span class="badge success">Posted</span>`
                        : `<span class="badge warning">Matched</span>`)
                    : `<span class="badge danger">Review</span>`}
                <div style="font-size:10px;color:#64748b;margin-top:3px;">${escapeHtml(r.reason)}</div>
            </td>
        </tr>`).join("");

    const tone = summary.unmatched === 0 ? "#16a34a" : summary.matched === 0 ? "#dc2626" : "#d97706";

    document.getElementById("rec-result").innerHTML = `
        <div style="background:${summary.unmatched === 0 ? "#dcfce7" : "#fef3c7"};border-left:3px solid ${tone};padding:12px;border-radius:6px;font-size:13px;margin-bottom:14px;color:#166534;">
            <strong>${summary.matched} of ${summary.total} matched</strong>
            ${summary.unmatched ? `, ${summary.unmatched} need review` : ""}.
            ${summary.posted ? ` ${summary.posted} posted to student accounts.` : ""}
            ${!postMatched ? "<br><small>Nothing was posted. Review the matches, then use 'Post matched payments'.</small>" : ""}
        </div>
        <div class="table-container">
            <table>
                <thead><tr>${headers.map(h => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;
}


const _showPageForMpesa = showPage;
showPage = function (pageId, clickedItem = null) {
    _showPageForMpesa(pageId, clickedItem);
    if (pageId === "mpesa") {
        loadMpesaPage();
    }
};// ===================== BILLING =====================

async function loadBilling() {
    try {
        const [summary, plans, invoices] = await Promise.all([
            fetch(apiUrl("/api/subscriptions/current")).then(r => r.json()),
            fetch(apiUrl("/api/subscriptions/plans")).then(r => r.json()),
            fetch(apiUrl("/api/subscriptions/invoices")).then(r => r.json())
        ]);

        renderBillingSummary(summary);
        renderBillingPlans(plans, summary.plan);
        billingInvoiceCache = invoices || [];
        renderBillingInvoices(billingInvoiceCache);
    } catch (error) {
        document.getElementById("billing-banner").innerHTML =
            `<div class="modal-error" style="margin-bottom:18px;">${escapeHtml(error.message)}</div>`;
    }
}


function renderBillingSummary(s) {
    // Only show a loud banner when the school actually cannot work normally.
    const banner = document.getElementById("billing-banner");
    if (s.subscription_status === "past_due" || s.subscription_status === "expired") {
        banner.innerHTML = `<div style="background:#fef3c7;border-left:3px solid #d97706;padding:12px;border-radius:6px;font-size:13px;color:#92400e;margin-bottom:18px;">
            <strong>Your subscription is ${escapeHtml(s.subscription_status.replace("_", " "))}.</strong>
            ${s.outstanding_balance > 0 ? ` KSh ${Number(s.outstanding_balance).toLocaleString()} is outstanding.` : ""}
            Please pay to keep your account active.
        </div>`;
    } else if (s.at_capacity) {
        banner.innerHTML = `<div style="background:#fef3c7;border-left:3px solid #d97706;padding:12px;border-radius:6px;font-size:13px;color:#92400e;margin-bottom:18px;">
            <strong>You are at your plan limit</strong> (${s.student_count}/${s.max_students} students). Upgrade to add more.
        </div>`;
    } else {
        banner.innerHTML = "";
    }

    document.getElementById("bill-plan").textContent = s.plan;
    document.getElementById("bill-price").textContent =
        s.monthly_price ? `KSh ${Number(s.monthly_price).toLocaleString()} per month` : "Free";

    document.getElementById("bill-students").textContent = s.student_count;
    document.getElementById("bill-capacity").textContent =
        s.max_students === null ? "unlimited allowed" : `of ${s.max_students} allowed`;

    document.getElementById("bill-outstanding").textContent = formatKsh(s.outstanding_balance);
    document.getElementById("bill-due").textContent = s.next_due_date
        ? `next due ${formatPaymentDate(s.next_due_date)}`
        : "nothing due";

    document.getElementById("bill-status").textContent = s.subscription_status;
    document.getElementById("bill-status-note").textContent =
        s.subscription_status === "active" ? "in good standing" : "action needed";
}


function renderBillingPlans(plans, currentPlan) {
    document.getElementById("billing-plans").innerHTML = (plans || []).map(p => {
        const isCurrent = p.code === currentPlan;
        const isEnterprise = p.code === "enterprise";
        const price = p.monthly_price === null
            ? (isEnterprise ? "Custom" : "Free")
            : `KSh ${Number(p.monthly_price).toLocaleString()}<span style="font-size:11px;color:#94a3b8;">/mo</span>`;
        const cap = p.max_students === null ? "Unlimited" : `${p.max_students} students`;

        return `
        <div class="plan-card ${isCurrent ? "current" : ""}">
            ${isCurrent ? `<span class="plan-badge">Your plan</span>` : ""}
            <h3>${escapeHtml(p.name)}</h3>
            <div class="plan-price">${price}</div>
            <div class="plan-cap">${escapeHtml(cap)}</div>
            <ul class="plan-features">
                ${p.features.map(f => `<li>${escapeHtml(f)}</li>`).join("")}
            </ul>
            ${isCurrent
                ? `<button class="small-btn" disabled>Current plan</button>`
                : isEnterprise
                    ? `<button class="small-btn" onclick="alert('Please contact us on +254 700 000 000 to discuss Enterprise.')">Contact us</button>`
                    : `<button class="primary-btn" onclick="changeBillingPlan('${escapeHtml(p.code)}')">Switch to ${escapeHtml(p.name)}</button>`}
        </div>`;
    }).join("");
}


async function changeBillingPlan(plan) {
    const current = document.getElementById("bill-plan").textContent;
    if (!confirm(`Switch from ${current} to ${plan}?\n\nYour student limit changes immediately.`)) return;

    try {
        const response = await fetch(apiUrl(`/api/subscriptions/change-plan?plan=${encodeURIComponent(plan)}`), {
            method: "POST"
        });
        if (!response.ok) {
            throw new Error(await readApiError(response, "Could not change plan"));
        }
        const summary = await response.json();
        renderBillingSummary(summary);
        loadBilling();
    } catch (error) {
        alert(error.message);
    }
}


function invoiceBadge(status) {
    const map = { paid: "success", unpaid: "warning", overdue: "danger", waived: "success" };
    return `<span class="badge ${map[status] || ""}">${escapeHtml(status)}</span>`;
}


function renderBillingInvoices(invoices) {
    const tbody = document.getElementById("billing-tbody");

    if (!invoices.length) {
        tbody.innerHTML = `<tr><td colspan="8">No subscription invoices yet. You are on a free trial.</td></tr>`;
        return;
    }

    tbody.innerHTML = invoices.map(inv => `
        <tr>
            <td><strong>${escapeHtml(inv.invoice_number)}</strong></td>
            <td>${formatPaymentDate(inv.period_start)} – ${formatPaymentDate(inv.period_end)}</td>
            <td>${escapeHtml(inv.plan)}</td>
            <td>${formatPaymentDate(inv.due_date)}</td>
            <td>${formatKsh(inv.amount_due)}</td>
            <td>${Number(inv.balance) > 0 ? formatKsh(inv.balance) : "—"}</td>
            <td>${invoiceBadge(inv.status)}</td>
            <td>
                ${inv.payments.length
                    ? `<button class="small-btn" onclick="showInvoicePayments(${inv.id})">${inv.payments.length} payment(s)</button>`
                    : "-"}
            </td>
        </tr>`).join("");
}


function showInvoicePayments(invoiceId) {
    const invoice = billingInvoiceCache.find(i => i.id === invoiceId);
    if (!invoice) return;

    const panel = document.getElementById("platform-billing");
    panel.style.display = "block";
    document.getElementById("platform-billing-note").textContent =
        `${invoice.invoice_number} — paid ${formatKsh(invoice.amount_paid)} of ${formatKsh(invoice.amount_due)}`;

    document.getElementById("platform-billing-detail").innerHTML = `
        <div class="table-container"><table>
            <thead><tr><th>Date</th><th>Amount</th><th>Method</th><th>Reference</th><th>Recorded by</th></tr></thead>
            <tbody>
                ${invoice.payments.map(p => `
                    <tr>
                        <td>${p.paid_at ? escapeHtml(p.paid_at.slice(0, 10)) : "-"}</td>
                        <td>${formatKsh(p.amount)}</td>
                        <td>${escapeHtml(p.method)}</td>
                        <td>${escapeHtml(p.reference || "-")}</td>
                        <td>${escapeHtml(p.recorded_by || "-")}</td>
                    </tr>`).join("")}
            </tbody>
        </table></div>`;
}

let billingInvoiceCache = [];

const _showPageForBilling = showPage;
showPage = function (pageId, clickedItem = null) {
    _showPageForBilling(pageId, clickedItem);
    if (pageId === "billing") {
        loadBilling();
    }
};