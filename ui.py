from aqt import mw
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QSlider, QPushButton,
    QCheckBox, QMessageBox, QSizePolicy
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

from .core import (
    fetch_cards, get_card_data, simulate_review_timeline,
    compute_due_matrix, sum_matrix_columns, apply_transformed_due_dates
)
from .tag_input_widget import TagInputWidget
from datetime import date, timedelta
import os
from .core import shuffle_new_cards, set_all_to_new


def build_chart_html(hist, labels):
    chart_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "chart.min.js"))
    with open(chart_path, "r", encoding="utf-8") as f:
        chartjs = f.read()

    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Time Warp Graph</title>
    <script>{chartjs}</script>
</head>
<body style="margin:0;">
<canvas id="timeWarpChart" style="width:100%; height:100vh;"></canvas>
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
                barThickness: 12
            }}
        ]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        scales: {{
            y: {{
                beginAtZero: true,
                title: {{ display: true, text: 'Cards Due' }}
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

def launch_timewarp():
    card_data_transformed = []

    dialog = QDialog()
    dialog.setWindowTitle("Anki Time Warp")
    dialog.setSizeGripEnabled(True)
    layout = QVBoxLayout(dialog) 
    logo_label = QLabel()
    
    addon_dir = os.path.dirname(__file__)
    logo_path = os.path.join(addon_dir, "logo.png")

    pixmap = QPixmap(logo_path)
    if not pixmap.isNull():
       pixmap = pixmap.scaled(100, 100)
       logo_label.setPixmap(pixmap)
    logo_label.setFixedSize(100, 100)

    logo_layout = QHBoxLayout()
    logo_layout.addStretch()
    logo_layout.addWidget(logo_label)

    layout.addLayout(logo_layout)


    deck_select = QComboBox()
    deck_names = ["All"] + [d.name for d in mw.col.decks.all_names_and_ids()]
    deck_select.addItems(deck_names)

    tag_widget = TagInputWidget(mw.col.tags.all())

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

    webview = QWebEngineView()
    webview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    layout.addWidget(QLabel("Select Deck:"))
    layout.addWidget(deck_select)
    layout.addWidget(QLabel("Tags:"))
    layout.addWidget(tag_widget)
    layout.addWidget(slider_stretch_label)
    layout.addWidget(slider_stretch)
    layout.addWidget(slider_shift_label)
    layout.addWidget(slider_shift)
    layout.addWidget(checkbox_collapse_overdues)
    layout.addWidget(checkbox_shuffle)
    layout.addWidget(checkbox_set_new)
    layout.addWidget(reset_btn)
    layout.addWidget(card_count_label)
    layout.addWidget(review_count_label)
    layout.addWidget(QLabel("Select Export Mode:"))
    layout.addWidget(export_mode_select)
    layout.addWidget(preview_btn)
    layout.addWidget(apply_changes_btn)
    layout.addWidget(webview)

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

        # Prepare debug output
        changes_preview = []
        for entry in card_data_transformed:
            original = entry["original_due"]
            new = entry["due"]
            changes_preview.append(f"{{cardID: {entry['cid']}, original: {original}, new: {new}}}")

        print("\n\nPending changes to be applied:")
        print("\n".join(changes_preview))

        if mode == "Write to current deck":
            reply = QMessageBox.question(
                dialog,
                "Review Changes",
                "You are about to introduce changes into the review data of the selected deck. You"
                " can undo the changes (Edit > Undo time warp) but they become permanent once you"
                " sync. Proceed?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                apply_transformed_due_dates(card_data_transformed)
                if checkbox_shuffle.isChecked():
                    shuffle_cards(card_data_transformed)
                if checkbox_set_new.isChecked():
                    set_cards_as_new(card_data_transformed)
                mw.reset()
                QMessageBox.information(dialog, "Success", "Review dates have been updated.")

        elif mode == "Create filtered deck":
            reply = QMessageBox.question(
                dialog,
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
                QMessageBox.information(dialog, "Filtered Deck Created", "Filtered deck with transformed due dates has been created.")

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

    dialog.setLayout(layout)
    dialog.resize(1000, 900)
    dialog.exec()

