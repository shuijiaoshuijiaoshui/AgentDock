/* 演示 portal：侧边抽屉内嵌客服（同源 iframe）+ 独立弹窗。 */

const $ = (id) => document.getElementById(id);

const panel = $("support-panel");
const frame = $("support-frame");

function openSupport() {
  // 懒加载：首次打开才加载客服页，避免无关页面也建会话
  if (!frame.getAttribute("src")) {
    frame.setAttribute("src", frame.dataset.src || "customer");
  }
  panel.classList.remove("hidden");
  $("launcher").classList.add("hidden");
}

function closeSupport() {
  panel.classList.add("hidden");
  $("launcher").classList.remove("hidden");
}

function openPopup() {
  const url = new URL("customer", document.baseURI).href;
  window.open(url, "agentdock-support",
    "width=480,height=780,menubar=no,toolbar=no,location=no,status=no");
}

for (const node of document.querySelectorAll("[data-support]")) {
  node.addEventListener("click", openSupport);
}
$("close-support").addEventListener("click", closeSupport);
$("open-popup").addEventListener("click", openPopup);
$("panel-popup").addEventListener("click", openPopup);
$("tile-popup").addEventListener("click", openPopup);
$("launcher").addEventListener("click", openSupport);

for (const item of document.querySelectorAll(".nav-item[data-view]")) {
  item.addEventListener("click", () => {
    for (const other of document.querySelectorAll(".nav-item")) {
      other.classList.remove("active");
    }
    item.classList.add("active");
    if (item.dataset.view !== "home") {
      $("content").querySelector(".note").textContent =
        "演示壳：该模块未接入业务数据；请从「在线客服」体验真实链路。";
    }
  });
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeSupport();
});
