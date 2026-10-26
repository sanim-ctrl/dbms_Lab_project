// main.js -- small UI enhancements only. No data is stored here.

document.addEventListener("DOMContentLoaded", function () {
  // Auto-select the donor's registered blood group on the donation form.
  // This is a convenience only; the server validates the match regardless.
  var donorSelect = document.getElementById("donorSelect");
  var bgSelect = document.getElementById("bgSelect");
  if (donorSelect && bgSelect) {
    donorSelect.addEventListener("change", function () {
      var opt = donorSelect.options[donorSelect.selectedIndex];
      var bg = opt ? opt.getAttribute("data-bg") : "";
      if (bg) {
        bgSelect.value = bg;
      }
    });
  }
});
