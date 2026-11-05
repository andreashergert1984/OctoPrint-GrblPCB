/*
 * Translated display names for layer and operation types.
 *
 * The backend stores technical identifiers (layer_type, operation_type);
 * everything the user reads is worded here so it can be translated.
 */
(function(global) {
    "use strict";

    function layer(type) {
        switch (type) {
            case "top_copper": return gettext("Top copper");
            case "bottom_copper": return gettext("Bottom copper");
            case "top_silk": return gettext("Top silkscreen");
            case "bottom_silk": return gettext("Bottom silkscreen");
            case "top_mask": return gettext("Top solder mask");
            case "bottom_mask": return gettext("Bottom solder mask");
            case "outline": return gettext("Outline");
            case "drill": return gettext("Drill holes");
            default: return type || "";
        }
    }

    function operationType(type) {
        switch (type) {
            case "isolation": return gettext("Isolation");
            case "drill": return gettext("Drilling");
            case "outline": return gettext("Outline cut");
            case "mask_expose": return gettext("Mask exposure");
            default: return type || "";
        }
    }

    // op: {operation_type, layer_type, name}
    function operation(op) {
        if (!op) return "";
        var label = operationType(op.operation_type);
        if (!label) return op.name || "";
        if (op.operation_type === "isolation" || op.operation_type === "mask_expose") {
            label += " (" + layer(op.layer_type) + ")";
        }
        if (op.mirrored) {
            label += " " + gettext("(mirrored)");
        }
        return label;
    }

    // GRBL 1.1 alarm codes
    function alarm(code) {
        switch (code) {
            case 1: return gettext("Hard limit triggered. Position is likely lost.");
            case 2: return gettext("Motion target exceeds machine travel.");
            case 3: return gettext("Reset while in motion. Position is likely lost.");
            case 4: return gettext("Probe fail: the probe was already triggered before probing.");
            case 5: return gettext("Probe fail: no contact within the programmed travel.");
            case 6: return gettext("Homing fail: reset during the homing cycle.");
            case 7: return gettext("Homing fail: safety door opened during homing.");
            case 8: return gettext("Homing fail: pull-off did not clear the limit switch.");
            case 9: return gettext("Homing fail: limit switch not found within search distance.");
            case 10: return gettext("Homing fail: second limit switch of a dual axis did not trigger.");
            default: return "";
        }
    }

    global.GrblPcbLabels = {
        alarm: alarm,
        layer: layer,
        operationType: operationType,
        operation: operation
    };
})(window);
