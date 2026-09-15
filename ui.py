from aqt import mw
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QSlider, QPushButton,
    QCheckBox, QMessageBox, QSizePolicy, QScrollArea, QWidget, QSpinBox
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

from .core import (
    fetch_cards, get_card_data, oldest_review_due, simulate_review_timeline,
    timeline_histogram, apply_transformed_due_dates
)
from .tag_input_widget import TagInputWidget
import os

# Fixed part of the horizon; the past part grows with the oldest overdue card.
MIN_HORIZON_PAST = 30
HORIZON_FUTURE = 90
SHIFT_LIMIT = 365

# Prevent multiple instances
dialog_instance = None


def build_chart_html(hist, labels, max_cap=0, y_max=None):
    chart_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "chart.min.js"))
    with open(chart_path, "r", encoding="utf-8") as f:
        chartjs = f.read()

    cap_js_dataset = ""
    if max_cap and int(max_cap) > 0:
        cap_values = ",".join([str(int(max_cap)) for _ in range(len(labels))])
        cap_js_dataset = f""",
            {{
                type: 'line',
                label: 'Cap',
                data: [{cap_values}],
                borderDash: [6,4],
                fill: false,
                pointRadius: 0,
                borderWidth: 1
            }}"""

    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Time Warp Graph</title>
    <script>{chartjs}</script>
    <style>
        body {{
            margin: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
        }}
        #container {{
            width: 1000px;
            height: 400px;
        }}
    </style>
</head>
<body>
<div id="container">
<canvas id="timeWarpChart" width="1000" height="400"></canvas>
</div>
<script>
const ctx = document.getElementById('timeWarpChart').getContext('2d');
new Chart(ctx, {{
    type: 'bar',
    data: {{
        labels: {labels},
        datasets: [
            {{
                label: 'Cards',
                data: {hist},
                backgroundColor: function(context) {{
                    const index = context.dataIndex;
                    const label = context.chart.data.labels[index];
                    return parseInt(label) < 0 ? 'rgba(255, 0, 0, 0.8)' : 'rgba(0, 123, 255, 0.8)';
                }},
                barThickness: 'flex',
                maxBarThickness: 10,
                categoryPercentage: 1.0,
                barPercentage: 0.8
            }}
            {cap_js_dataset}
        ]
    }},
    options: {{
        responsive: false,
        maintainAspectRatio: false,
        animation: false,
        plugins: {{
            legend: {{ display: false }},
            tooltip: {{
                callbacks: {{
                    label: function(context) {{
                        const value = context.raw;
                        const day = context.label;
                        return `${{value}} cards due on Day ${{day}}`;
                    }}
                }}
            }}
        }},
        scales: {{
            x: {{
                title: {{
                    display: true,
                    text: 'Day Offset (0 = Today)'
                }},
                ticks: {{ autoSkip: true, maxTicksLimit: 40, maxRotation: 0 }}
            }},
            y: {{
                title: {{
                    display: true,
                    text: 'Number of Cards'
                }},
                beginAtZero: true{', max: ' + str(y_max) if y_max else ''}
            }}
        }}
    }}
}});
</script>
</body>
</html>
"""


def clear_dialog_instance():
    global dialog_instance
    dialog_instance = None


def _slider_with_spinbox(minimum, maximum, suffix):
    """Horizontal slider + spinbox kept in sync; returns (layout, slider, spin)."""
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(minimum, maximum)
    slider.setValue(0)
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(0)
    spin.setSuffix(suffix)
    spin.setFixedWidth(110)
    slider.valueChanged.connect(spin.setValue)
    spin.valueChanged.connect(slider.setValue)
    row = QHBoxLayout()
    row.addWidget(slider)
    row.addWidget(spin)
    return row, slider, spin


def launch_timewarp():
    global dialog_instance
    if dialog_instance is not None and dialog_instance.isVisible():
        dialog_instance.raise_()
        dialog_instance.activateWindow()
        return

    # State shared between the closures below
    state = {
        "card_data": [],
        "horizon_past": MIN_HORIZON_PAST,
        "y_max": 0,
    }

    dialog_instance = QDialog()
    dialog_instance.setWindowTitle("Anki Time Warp")
    dialog_instance.setSizeGripEnabled(True)
    screen_geometry = mw.app.primaryScreen().availableGeometry()
    dialog_instance.resize(1000, int(screen_geometry.height() * 0.95))
    main_layout = QVBoxLayout(dialog_instance)

    scroll_area = QScrollArea()
    scroll_area.setWidgetResizable(True)
    scroll_content = QWidget()
    scroll_content.setMaximumWidth(1000)
    scroll_content.setMinimumWidth(1000)
    scroll_content.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
    scroll_layout = QVBoxLayout(scroll_content)

    top_layout = QHBoxLayout()
    deck_tag_container = QVBoxLayout()

    deck_select_label = QLabel("Select Deck:")
    deck_select = QComboBox()
    deck_names = ["All"] + [d.name for d in mw.col.decks.all_names_and_ids()]
    deck_select.addItems(deck_names)
    deck_select.setFixedWidth(800)

    tag_widget_label = QLabel("Tags:")
    tag_widget = TagInputWidget(mw.col.tags.all())
    tag_widget.setFixedWidth(800)

    deck_tag_container.addWidget(deck_select_label)
    deck_tag_container.addWidget(deck_select)
    deck_tag_container.addWidget(tag_widget_label)
    deck_tag_container.addWidget(tag_widget)

    top_layout.addLayout(deck_tag_container)

    logo_label = QLabel()
    addon_dir = os.path.dirname(__file__)
    logo_path = os.path.join(addon_dir, "logo.png")
    pixmap = QPixmap(logo_path)
    if not pixmap.isNull():
        pixmap = pixmap.scaled(100, 100)
        logo_label.setPixmap(pixmap)
    logo_label.setFixedSize(100, 100)
    top_layout.addStretch()
    top_layout.addWidget(logo_label)

    scroll_layout.addLayout(top_layout)

    stretch_label = QLabel("Stretch (+ flattens toward an even load, − compresses toward today):")
    stretch_row, slider_stretch, spin_stretch = _slider_with_spinbox(-100, 500, " %")

    shift_label = QLabel("Shift (move the whole schedule; + = later):")
    shift_row, slider_shift, spin_shift = _slider_with_spinbox(-SHIFT_LIMIT, SHIFT_LIMIT, " days")

    checkbox_collapse_overdues = QCheckBox("Collapse overdues to T0")

    # 0 = auto-level (flatten to average), -1 = unlimited, >0 = manual cap
    max_per_day_label = QLabel("Max cards/day:")
    max_per_day_spin = QSpinBox()
    max_per_day_spin.setRange(-1, 100000)
    max_per_day_spin.setValue(-1)
    max_per_day_spin.setToolTip("-1 = off (stretch controls distribution).\n0 = auto-flatten.\n>0 = manual cap per day.")

    card_count_label = QLabel("Cards in scope: 0")
    review_count_label = QLabel("Review cards: 0")
    overdue_info_label = QLabel("")

    reset_btn = QPushButton("Reset Sliders")
    preview_btn = QPushButton("Preview")
    apply_changes_btn = QPushButton("Apply Changes")

    scroll_layout.addWidget(stretch_label)
    scroll_layout.addLayout(stretch_row)
    scroll_layout.addWidget(shift_label)
    scroll_layout.addLayout(shift_row)
    scroll_layout.addWidget(checkbox_collapse_overdues)
    scroll_layout.addWidget(max_per_day_label)
    scroll_layout.addWidget(max_per_day_spin)
    scroll_layout.addWidget(reset_btn)
    scroll_layout.addWidget(card_count_label)
    scroll_layout.addWidget(review_count_label)
    scroll_layout.addWidget(overdue_info_label)
    scroll_layout.addWidget(preview_btn)
    scroll_layout.addWidget(apply_changes_btn)

    scroll_area.setWidget(scroll_content)
    main_layout.addWidget(scroll_area)

    webview = QWebEngineView()
    webview.setFixedSize(1000, 400)
    main_layout.addWidget(webview)

    # Debounce: chart only redraws after 200 ms of inactivity
    debounce_timer = QTimer()
    debounce_timer.setSingleShot(True)
    debounce_timer.setInterval(200)

    def schedule_update():
        debounce_timer.start()

    def reset_y_axis_and_update():
        state["y_max"] = 0
        schedule_update()

    def update_graph():
        today = mw.col.sched.today
        deck = deck_select.currentText()
        tags = tag_widget.get_tags()
        stretch = slider_stretch.value()
        shift = slider_shift.value()
        collapse_overdues = checkbox_collapse_overdues.isChecked()
        max_cap = int(max_per_day_spin.value())

        cids = fetch_cards(deck, tags)
        card_count_label.setText(f"Cards in scope: {len(cids)}")
        card_data = get_card_data(cids)

        # Past horizon grows so that the oldest overdue card still fits.
        oldest = oldest_review_due(card_data)
        horizon_past = MIN_HORIZON_PAST
        if oldest is not None:
            horizon_past = max(MIN_HORIZON_PAST, today - oldest + 1)
        state["horizon_past"] = horizon_past

        n_review = sum(1 for c in card_data if c["type"] == "review")
        n_overdue = sum(1 for c in card_data if c["type"] == "review" and c["due"] < today)

        state["card_data"] = simulate_review_timeline(
            card_data,
            stretch_pct=stretch,
            shift=shift,
            horizon_past=horizon_past,
            horizon_future=HORIZON_FUTURE,
            collapse_overdues=collapse_overdues,
            max_cards_per_day=max_cap,
        )

        hist = timeline_histogram(state["card_data"])
        assigned = sum(hist)
        review_count_label.setText(f"Review cards: {n_review}  (placed: {assigned})")
        if oldest is not None and n_overdue:
            overdue_info_label.setText(
                f"Overdue now: {n_overdue}, oldest {today - oldest} days")
        else:
            overdue_info_label.setText("Overdue now: 0")

        # Chart covers the whole simulated timeline (past part is dynamic,
        # future part may exceed HORIZON_FUTURE after shift / cap).
        base_range = horizon_past + HORIZON_FUTURE
        chart_hist = list(hist)
        while len(chart_hist) < base_range:
            chart_hist.append(0)

        # Y-axis stability: only rescale upward when peak > 75% of current max
        current_peak = max(chart_hist) if chart_hist else 0
        if state["y_max"] == 0:
            state["y_max"] = max(1, int(current_peak * 1.1))
        elif current_peak > state["y_max"] * 0.75:
            state["y_max"] = max(state["y_max"], int(current_peak * 1.1))

        labels = [str(i - horizon_past) for i in range(len(chart_hist))]
        html = build_chart_html(chart_hist, labels, max_cap=max_cap, y_max=state["y_max"])
        webview.setHtml(html)

    def apply_changes():
        # Always simulate from the current widget state; the last preview
        # may be stale (tags changed, debounce pending, or no preview yet).
        debounce_timer.stop()
        update_graph()
        card_data = state["card_data"]

        n_changed = sum(1 for c in card_data
                        if c["type"] == "review" and c["due"] != c["original_due"])
        if n_changed == 0:
            QMessageBox.information(dialog_instance, "Nothing to do",
                                    "No due dates would change with the current settings.")
            return

        reply = QMessageBox.question(
            dialog_instance,
            "Review Changes",
            f"This will rewrite the due date of {n_changed} review cards in the selected"
            " scope. Undo is possible until you sync. Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        updated, skipped = apply_transformed_due_dates(card_data, state["horizon_past"])
        mw.reset()
        msg = f"{updated} review dates have been updated. Undo via Edit > Undo Time Warp."
        if skipped:
            msg += f"\n\nWarning: {skipped} review cards were outside the simulated range and were left untouched."
        QMessageBox.information(dialog_instance, "Success", msg)
        update_graph()

    def reset_sliders():
        slider_stretch.setValue(0)
        slider_shift.setValue(0)
        state["y_max"] = 0

    debounce_timer.timeout.connect(update_graph)

    slider_stretch.valueChanged.connect(schedule_update)
    slider_shift.valueChanged.connect(schedule_update)
    max_per_day_spin.valueChanged.connect(schedule_update)
    deck_select.currentIndexChanged.connect(reset_y_axis_and_update)
    tag_widget.tagChanged.connect(reset_y_axis_and_update)
    checkbox_collapse_overdues.stateChanged.connect(schedule_update)
    preview_btn.clicked.connect(update_graph)       # Preview = immediate
    reset_btn.clicked.connect(reset_sliders)
    apply_changes_btn.clicked.connect(apply_changes)

    dialog_instance.setLayout(main_layout)
    dialog_instance.finished.connect(lambda: clear_dialog_instance())
    dialog_instance.exec()
