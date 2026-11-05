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
        if (/\(mirrored\)/i.test(op.name || "")) {
            label += " " + gettext("(mirrored)");
        }
        return label;
    }

    global.GrblPcbLabels = {
        layer: layer,
        operationType: operationType,
        operation: operation
    };
})(window);
