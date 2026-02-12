const extractBtn = document.getElementById("extractBtn");
const fileInput = document.getElementById("fileInput");
const output = document.getElementById("output");60


extractBtn.addEventListener("click", () => {
  const file = fileInput.files[0];

  if (!file) {
    alert("Please select a file first");
    return;
  }

  // Dummy JSON for now (backend later)
  const result = {
    patient: {
      name: "—",
      age: "—",
      ward_no: "—",
      doctor: "—"
    },
    admitted_date: "—",
    discharged_date: "—",
    treatment_given: [],
    drug_advice: []
  };

  output.textContent = JSON.stringify(result, null, 2);
});
