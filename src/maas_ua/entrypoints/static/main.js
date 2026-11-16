// Main JavaScript file as helper for the index.html file. This file is loaded at the bottom of the index.html file.
function currentMonthAndYear() {
    const now = new Date();
    const month = now.toLocaleString('default', { month: 'long' });
    const year = now.getFullYear();
    return `${month} ${year}`;
}

function updateCurrentMonthAndYear() {
    const currentMonthAndYearElement = document.getElementById('current-month-and-year');
    if (currentMonthAndYearElement) {
        currentMonthAndYearElement.textContent = currentMonthAndYear();
    }
}

function updateCurrentMonthAndYearInputs() {
    const now = new Date();
    const monthInput = document.getElementById('month');
    const yearInput = document.getElementById('year');

    if (monthInput) {
        monthInput.value = String(now.getMonth() + 1);
    }

    if (yearInput) {
        yearInput.value = String(now.getFullYear());
    }
}

// Call the function to update the current month and year when the page loads
document.addEventListener('DOMContentLoaded', () => {
    updateCurrentMonthAndYear();
    updateCurrentMonthAndYearInputs();
});