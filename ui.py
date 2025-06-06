from aqt import mw
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QSlider, QPushButton,
    QCheckBox, QMessageBox, QSizePolicy, QScrollArea, QWidget
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

from .core import (
    fetch_cards, get_card_data, simulate_review_timeline,
    compute_due_matrix, sum_matrix_columns, apply_transformed_due_dates
)
from .tag_input_widget import TagInputWidget
from datetime import date
import os
from .core import shuffle_new_cards as shuffle_cards, set_all_to_new as set_cards_as_new

# Prevent multiple instances
dialog_instance = None

def build_chart_html(hist, labels):
    chart_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "chart.min.js"))
    with open(chart_path, "r", encoding="utf-8") as f:
        chartjs = f.read()

    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset=\"UTF-8\">
    <title>Time Warp Graph</title>
    <script>{chartjs}</script>
    <style>
        body {{
            margin: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100%;
        }}
    </style>
</head>
<body>
<canvas id=\"timeWarpChart\" width=\"1000\" height=\"400\"></canvas>
<script>
const ctx = document.getElementById('timeWarpChart').getContext('2d');
new Chart(ctx, {{
    type: 'bar',
    data: {{
        labels: {labels},
        datasets: [
            {{
                label: 'Overdue',
                data: {hist},
                backgroundColor: function(context) {{
                    const index = context.dataIndex;
                    const label = context.chart.data.labels[index];
                    return parseInt(label) < 0 ? 'rgba(255, 0, 0, 0.8)' : 'rgba(0, 123, 255, 0.8)';
                }},
                barThickness: 10
            }}
        ]
    }},
    options: {{
        responsive: false,
        maintainAspectRatio: false,
        plugins: {{
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
            y: {{
                beginAtZero: true,
                title: {{
                    display: true,
                    text: 'Cards Due'
                }}
            }}
        }}
    }}
}});
</script>
</body>
</html>
"""

def create_filtered_deck_from_transformed(card_data):
    deck_name = "TimeWarpFiltered"
    deck_id = mw.col.decks.id(deck_name)
    mw.col.decks.select(deck_id)
    mw.col.sched.unbury_cards()
    mw.col.decks.get(deck_id)["dyn"] = True
    mw.col.decks.get(deck_id)["terms"] = [[1, "cid:" + " OR cid:".join(str(card["cid"]) for card in card_data), 0]]
    mw.col.decks.get(deck_id)["resched"] = True
    mw.col.decks.save(deck_id)
    mw.col.sched.rebuild_filtered_deck(deck_id)

def clear_dialog_instance():
    global dialog_instance
    dialog_instance = None

def launch_timewarp():
    global dialog_instance
    if dialog_instance is not None and dialog_instance.isVisible():
        dialog_instance.raise_()
        dialog_instance.activateWindow()
        return

    card_data_transformed = []

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

    slider_stretch = QSlider(Qt.Orientation.Horizontal)
    slider_stretch.setMinimum(-100)
    slider_stretch.setMaximum(500)
    slider_stretch.setValue(0)
    slider_stretch_label = QLabel("Stretch: 0%")

    slider_shift = QSlider(Qt.Orientation.Horizontal)
    slider_shift.setMinimum(-30)
    slider_shift.setMaximum(30)
    slider_shift.setValue(0)
    slider_shift_label = QLabel("Shift: 0 days")

    checkbox_collapse_overdues = QCheckBox("Collapse overdues to T0")
    checkbox_shuffle = QCheckBox("Shuffle new cards on export")
    checkbox_set_new = QCheckBox("Set all cards to new")

    card_count_label = QLabel("Cards in scope: 0")
    review_count_label = QLabel("Cards currently in review: 0")

    export_mode_select = QComboBox()
    export_mode_select.addItems(["Write to current deck", "Create filtered deck"])

    reset_btn = QPushButton("Reset Sliders")
    preview_btn = QPushButton("Preview")
    apply_changes_btn = QPushButton("Apply Changes")

    scroll_layout.addWidget(slider_stretch_label)
    scroll_layout.addWidget(slider_stretch)
    scroll_layout.addWidget(slider_shift_label)
    scroll_layout.addWidget(slider_shift)
    scroll_layout.addWidget(checkbox_collapse_overdues)
    scroll_layout.addWidget(checkbox_shuffle)
    scroll_layout.addWidget(checkbox_set_new)
    scroll_layout.addWidget(reset_btn)
    scroll_layout.addWidget(card_count_label)
    scroll_layout.addWidget(review_count_label)
    scroll_layout.addWidget(QLabel("Select Export Mode:"))
    scroll_layout.addWidget(export_mode_select)
    scroll_layout.addWidget(preview_btn)
    scroll_layout.addWidget(apply_changes_btn)

    scroll_area.setWidget(scroll_content)
    main_layout.addWidget(scroll_area)

    webview = QWebEngineView()
    webview.setFixedSize(1000, 400)
    main_layout.addWidget(webview)

    def update_labels():
        slider_stretch_label.setText(f"Stretch: {slider_stretch.value()}%")
        slider_shift_label.setText(f"Shift: {slider_shift.value()} days")

    def update_graph():
        nonlocal card_data_transformed
        horizon_past = 30
        horizon_future = 90
        total_horizon = horizon_past + horizon_future

        deck = deck_select.currentText()
        tags = tag_widget.get_tags()
        stretch = slider_stretch.value()
        shift = slider_shift.value()
        collapse_overdues = checkbox_collapse_overdues.isChecked()

        cids = fetch_cards(deck, tags)
        card_count_label.setText(f"Cards in scope: {len(cids)}")

        card_data = get_card_data(cids)
        card_data_transformed = simulate_review_timeline(
            card_data, stretch_pct=stretch, shift=shift,
            horizon_past=horizon_past, horizon_future=horizon_future,
            collapse_overdues=collapse_overdues
        )
        matrix_transformed = compute_due_matrix(card_data_transformed, total_horizon)
        hist_transformed = sum_matrix_columns(matrix_transformed)
        review_count_label.setText(f"Cards currently in review: {sum(hist_transformed)}")

        labels = [str(i - horizon_past) for i in range(total_horizon)]
        html = build_chart_html(hist_transformed, labels)
        webview.setHtml(html)

    def apply_changes():
        today = date.today()
        mode = export_mode_select.currentText()

        changes_preview = []
        for entry in card_data_transformed:
            original = entry["original_due"]
            new = entry["due"]
            changes_preview.append(f"{{cardID: {entry['cid']}, original: {original}, new: {new}}}")

        print("\n\nPending changes to be applied:")
        print("\n".join(changes_preview))

        if mode == "Write to current deck":
            reply = QMessageBox.question(
                dialog_instance,
                "Review Changes",
                "You are about to introduce changes into the review data of the selected deck."
                " Undoing the changes is possible until you sync. Proceed?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                apply_transformed_due_dates(card_data_transformed)
                if checkbox_shuffle.isChecked():
                    shuffle_cards(card_data_transformed)
                if checkbox_set_new.isChecked():
                    set_cards_as_new(card_data_transformed)
                mw.reset()
                QMessageBox.information(
                    dialog_instance,
                    "Success",
                    "Review dates have been updated. Undo from (Edit > Undo Time Warp)",
                )

        elif mode == "Create filtered deck":
            reply = QMessageBox.question(
                dialog_instance,
                "Filtered Deck",
                "Reviewing cards in the filtered deck will introduce permanent changes in their review timeline. Proceed?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                create_filtered_deck_from_transformed(card_data_transformed)
                if checkbox_shuffle.isChecked():
                    shuffle_cards(card_data_transformed)
                if checkbox_set_new.isChecked():
                    set_cards_as_new(card_data_transformed)
                mw.reset()
                QMessageBox.information(dialog_instance, "Filtered Deck Created", "Filtered deck with transformed due dates has been created.")

    def reset_sliders():
        slider_stretch.setValue(0)
        slider_shift.setValue(0)

    slider_stretch.valueChanged.connect(update_labels)
    slider_shift.valueChanged.connect(update_labels)
    slider_stretch.valueChanged.connect(update_graph)
    slider_shift.valueChanged.connect(update_graph)
    deck_select.currentIndexChanged.connect(update_graph)
    checkbox_collapse_overdues.stateChanged.connect(update_graph)
    preview_btn.clicked.connect(update_graph)
    reset_btn.clicked.connect(reset_sliders)
    apply_changes_btn.clicked.connect(apply_changes)

    dialog_instance.setLayout(main_layout)
    dialog_instance.finished.connect(lambda: clear_dialog_instance())
    dialog_instance.exec()
