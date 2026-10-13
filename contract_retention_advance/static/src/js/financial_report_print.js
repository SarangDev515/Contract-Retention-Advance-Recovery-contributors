/** @odoo-module **/
import { registry } from "@web/core/registry";

let activePrintFrame = null;

registry.category("actions").add("cra_print_financial_report", (env, action) => {
    const url = new URL(action.params.url, window.location.origin);
    if (url.origin !== window.location.origin || !url.pathname.startsWith("/report/html/contract_retention_advance.financial_report_print/")) {
        throw new Error("Invalid contract report print URL");
    }
    if (activePrintFrame) {
        env.services.notification.add("The report print dialog is already loading or open.", { type: "info" });
        return;
    }
    const frame = document.createElement("iframe");
    frame.title = "Contract report print document";
    frame.setAttribute("aria-hidden", "true");
    frame.style.cssText = "position:fixed;left:-10000px;top:0;width:1123px;height:794px;border:0;pointer-events:none;";
    activePrintFrame = frame;
    const cleanup = () => {
        frame.remove();
        if (activePrintFrame === frame) activePrintFrame = null;
        window.removeEventListener("pagehide", cleanup);
    };
    const failure = () => {
        cleanup();
        env.services.notification.add("The report could not be loaded for printing. Check your Odoo connection and try again.", { type: "danger", sticky: true });
    };
    frame.addEventListener("error", failure, { once: true });
    frame.addEventListener("load", async () => {
        try {
            const printWindow = frame.contentWindow;
            const doc = frame.contentDocument;
            if (!doc || !doc.querySelector(".cra_print_report")) {
                failure();
                return;
            }
            if (doc.fonts) await doc.fonts.ready;
            await Promise.all(Array.from(doc.images, image => image.complete ? Promise.resolve() : new Promise(resolve => {
                image.addEventListener("load", resolve, { once: true });
                image.addEventListener("error", resolve, { once: true });
            })));
            // The QWeb report supplies @page { size: A4 landscape; }.
            // Keep the frame until the native dialog is closed or cancelled.
            printWindow.addEventListener("afterprint", cleanup, { once: true });
            printWindow.focus();
            printWindow.print();
        } catch {
            failure();
        }
    }, { once: true });
    window.addEventListener("pagehide", cleanup, { once: true });
    frame.src = url.href;
    document.body.appendChild(frame);
});
