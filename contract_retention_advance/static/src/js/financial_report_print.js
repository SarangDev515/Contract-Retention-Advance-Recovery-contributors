/** @odoo-module **/

import { onMounted, onPatched, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { FormController } from "@web/views/form/form_controller";
import { formView } from "@web/views/form/form_view";

/* ============================================================
 * FILTER PANEL: SMOOTH COLLAPSE / EXPAND
 * ============================================================ */

const FILTER_STYLE_ID = "cra-report-filter-toggle-styles";

function installFilterToggleStyles() {
    if (document.getElementById(FILTER_STYLE_ID)) {
        return;
    }

    const style = document.createElement("style");
    style.id = FILTER_STYLE_ID;

    style.textContent = `
        .cra_report_single_screen .cra_report_filter_slide {
            display: grid;
            grid-template-rows: 1fr;
            flex: 0 0 auto;
            min-height: 0;
            position: relative;
            z-index: 50;
            overflow: visible;
            opacity: 1;
            transition:
                grid-template-rows 280ms ease,
                opacity 220ms ease;
        }

        .cra_report_single_screen .cra_report_filter_slide_inner {
            min-height: 0;
            overflow: visible;
            transform: translateY(0);
            transition: transform 280ms ease;
        }

        .cra_report_single_screen
        .cra_report_filter_slide.cra_filters_collapsed {
            grid-template-rows: 0fr;
            opacity: 0;
        }

        .cra_report_single_screen
        .cra_filters_collapsed .cra_report_filter_slide_inner {
            overflow: hidden;
            transform: translateY(-18px);
        }

        .cra_report_single_screen .cra_report_filter_panel {
            overflow: visible !important;
        }

        .cra_report_single_screen
        .cra_filters_collapsed .cra_report_filter_panel {
            overflow: hidden !important;
        }

        .cra_report_single_screen
        .cra_report_filter_panel .o-autocomplete--dropdown-menu {
            z-index: 1055;
        }

        .cra_report_single_screen .cra_report_toggle_bar {
            display: flex;
            justify-content: flex-end;
            align-items: center;
            flex: 0 0 auto;
            min-height: 28px;
            margin: 2px 0;
        }

        .cra_report_single_screen .cra_report_filter_toggle {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 32px;
            height: 28px;
            padding: 0;
            border: 0;
            border-radius: 4px;
            background: transparent;
            color: #51436f;
            cursor: pointer;
        }

        .cra_report_single_screen .cra_report_filter_toggle:hover {
            background: #eee9f5;
        }

        .cra_report_single_screen
        .cra_report_filter_toggle:focus-visible {
            outline: 2px solid #71639e;
            outline-offset: 2px;
        }

        .cra_report_single_screen .cra_report_filter_toggle .fa {
            font-size: 18px;
        }

        @media (prefers-reduced-motion: reduce) {
            .cra_report_single_screen .cra_report_filter_slide,
            .cra_report_single_screen .cra_report_filter_slide_inner {
                transition: none;
            }
        }
    `;

    document.head.appendChild(style);
}

class ContractReportFormController extends FormController {
    setup() {
        super.setup();

        this.filtersCollapsed = false;
        this.filterToggleButton = null;
        this.filterToggleBar = null;

        this.onFilterToggle = () => {
            this.filtersCollapsed = !this.filtersCollapsed;
            this.applyFilterState();
        };

        onMounted(() => {
            installFilterToggleStyles();
            this.setupFilterToggle();
        });

        onPatched(() => {
            this.setupFilterToggle();
        });

        onWillUnmount(() => {
            this.removeFilterToggle();
        });
    }

    removeFilterToggle() {
        if (this.filterToggleButton) {
            this.filterToggleButton.removeEventListener(
                "click",
                this.onFilterToggle
            );
        }

        if (this.filterToggleBar) {
            this.filterToggleBar.remove();
        }

        this.filterToggleButton = null;
        this.filterToggleBar = null;
    }

    setupFilterToggle() {
        const root = this.rootRef.el;

        if (!root) {
            return;
        }

        const slide = root.querySelector(".cra_report_filter_slide");

        if (!slide) {
            return;
        }

        const existingControlIsValid =
            this.filterToggleBar &&
            this.filterToggleBar.isConnected &&
            slide.nextElementSibling === this.filterToggleBar;

        if (!existingControlIsValid) {
            this.removeFilterToggle();

            const toggleBar = document.createElement("div");
            toggleBar.className = "cra_report_toggle_bar";

            const button = document.createElement("button");
            button.type = "button";
            button.className = "cra_report_filter_toggle";

            const icon = document.createElement("i");
            icon.setAttribute("aria-hidden", "true");

            button.appendChild(icon);
            button.addEventListener("click", this.onFilterToggle);

            toggleBar.appendChild(button);
            slide.insertAdjacentElement("afterend", toggleBar);

            this.filterToggleButton = button;
            this.filterToggleBar = toggleBar;
        }

        this.applyFilterState();
    }

    applyFilterState() {
        const root = this.rootRef.el;
        const button = this.filterToggleButton;

        if (!root || !button) {
            return;
        }

        const slide = root.querySelector(".cra_report_filter_slide");

        if (!slide) {
            return;
        }

        slide.classList.toggle(
            "cra_filters_collapsed",
            this.filtersCollapsed
        );

        // Keep the fields mounted, but prevent keyboard access
        // while their panel is hidden.
        slide.inert = this.filtersCollapsed;
        slide.setAttribute(
            "aria-hidden",
            String(this.filtersCollapsed)
        );

        button.setAttribute(
            "aria-expanded",
            String(!this.filtersCollapsed)
        );

        const label = this.filtersCollapsed
            ? "Show report filters"
            : "Hide report filters";

        button.title = label;
        button.setAttribute("aria-label", label);

        const icon = button.querySelector("i");

        if (icon) {
            icon.className = this.filtersCollapsed
                ? "fa fa-eye"
                : "fa fa-eye-slash";
        }
    }
}

registry.category("views").add("cra_report_form", {
    ...formView,
    Controller: ContractReportFormController,
});

/* ============================================================
 * PRINT REPORT: HIDDEN FRAME, NO NEW TAB
 * ============================================================ */

let activePrintFrame = null;

registry.category("actions").add(
    "cra_print_financial_report",
    (env, action) => {
        const url = new URL(
            action.params.url,
            window.location.origin
        );

        if (
            url.origin !== window.location.origin ||
            !url.pathname.startsWith(
                "/report/html/contract_retention_advance.financial_report_print/"
            )
        ) {
            throw new Error("Invalid contract report print URL");
        }

        if (activePrintFrame) {
            env.services.notification.add(
                "The report print dialog is already loading or open.",
                { type: "info" }
            );
            return;
        }

        const frame = document.createElement("iframe");

        frame.title = "Contract report print document";
        frame.setAttribute("aria-hidden", "true");
        frame.style.cssText =
            "position:fixed;" +
            "left:-10000px;" +
            "top:0;" +
            "width:1123px;" +
            "height:794px;" +
            "border:0;" +
            "pointer-events:none;";

        activePrintFrame = frame;

        const cleanup = () => {
            frame.remove();

            if (activePrintFrame === frame) {
                activePrintFrame = null;
            }

            window.removeEventListener("pagehide", cleanup);
        };

        const failure = () => {
            cleanup();

            env.services.notification.add(
                "The report could not be loaded for printing. " +
                "Check your Odoo connection and try again.",
                {
                    type: "danger",
                    sticky: true,
                }
            );
        };

        frame.addEventListener("error", failure, { once: true });

        frame.addEventListener(
            "load",
            async () => {
                try {
                    const printWindow = frame.contentWindow;
                    const doc = frame.contentDocument;

                    if (
                        !printWindow ||
                        !doc ||
                        !doc.querySelector(".cra_print_report")
                    ) {
                        failure();
                        return;
                    }

                    if (doc.fonts) {
                        await doc.fonts.ready;
                    }

                    await Promise.all(
                        Array.from(doc.images, (image) => {
                            if (image.complete) {
                                return Promise.resolve();
                            }

                            return new Promise((resolve) => {
                                image.addEventListener(
                                    "load",
                                    resolve,
                                    { once: true }
                                );

                                image.addEventListener(
                                    "error",
                                    resolve,
                                    { once: true }
                                );

                                // Avoid waiting if the image finished
                                // between the initial check and listeners.
                                if (image.complete) {
                                    resolve();
                                }
                            });
                        })
                    );

                    // Landscape orientation is supplied by the
                    // existing QWeb report's A4 landscape @page rule.
                    printWindow.addEventListener(
                        "afterprint",
                        cleanup,
                        { once: true }
                    );

                    printWindow.focus();
                    printWindow.print();
                } catch {
                    failure();
                }
            },
            { once: true }
        );

        window.addEventListener(
            "pagehide",
            cleanup,
            { once: true }
        );

        frame.src = url.href;
        document.body.appendChild(frame);
    }
);