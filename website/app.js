(() => {
  "use strict";

  const year = document.querySelector("[data-current-year]");
  if (year) {
    year.textContent = String(new Date().getFullYear());
  }

  const mobileNavigation = document.querySelector(".mobile-nav");
  if (mobileNavigation) {
    mobileNavigation.querySelectorAll("a").forEach((link) => {
      link.addEventListener("click", () => {
        mobileNavigation.removeAttribute("open");
      });
    });
  }

  const copyButton = document.querySelector("[data-copy-target]");
  const copyStatus = document.querySelector(".copy-status");

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }

    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    if (!copied) {
      throw new Error("Copy command was rejected");
    }
  }

  if (copyButton) {
    copyButton.addEventListener("click", async () => {
      const targetId = copyButton.getAttribute("data-copy-target");
      const target = targetId ? document.getElementById(targetId) : null;
      if (!target) {
        return;
      }

      try {
        await copyText(target.textContent.trim());
        copyButton.textContent = "Copied";
        if (copyStatus) {
          copyStatus.textContent = "Command copied to clipboard.";
        }
      } catch {
        copyButton.textContent = "Select";
        if (copyStatus) {
          copyStatus.textContent = "Copy was unavailable. Select the command manually.";
        }
      }

      window.setTimeout(() => {
        copyButton.textContent = "Copy";
        if (copyStatus) {
          copyStatus.textContent = "";
        }
      }, 2200);
    });
  }
})();
