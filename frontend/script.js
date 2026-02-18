const extractBtn = document.getElementById("extractBtn");
const fileInput = document.getElementById("fileInput");
const output = document.getElementById("output");

extractBtn.addEventListener("click", () => {
  const file = fileInput.files[0];

  if (!file) {
    alert("Please select a file first");
    return;
  }

  const formData = new FormData();
  formData.append("file", file);

  output.textContent = "Uploading...";

  fetch("http://127.0.0.1:5000/upload", {
    method: "POST",
    body: formData
  })
    .then(async (res) => {
      const text = await res.text();
      if (!res.ok) throw new Error(text);
      return JSON.parse(text);
    })
    .then((data) => {
      output.textContent = JSON.stringify(data, null, 2);
    })
    .catch((err) => {
      output.textContent = "Upload failed:\n" + err.message;
      console.error(err);
    });
});
