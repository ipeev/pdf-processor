"""Regression: figure captions stay outside cropped source pixels.

The YOLOv3 page-1 bbox below is from the previously saved Haiku response;
it is test input, not a new paid model call. No external paper is bundled.
"""
from dataclasses import replace
from io import BytesIO
import json
from hashlib import sha256

from PIL import Image, ImageDraw

from app.cache import fingerprint
from app.config import Settings
from app.parsers import vision_figures
from app.parsers.vision_figures import FigureCandidate, assemble_page, padded_box
from app.schemas import ParserChoice


def _figure(raw=(506,250,911,421), caption=(506,433,911,475), nearby=((506,500,739,513),)):
    return FigureCandidate('figure-1','chart','Figure 1. Performance graph',raw,caption,nearby)


def test_yolov3_page1_same_saved_coordinates_leave_caption_clearance():
    # The crop must stop before the separately annotated caption.
    fig = _figure()
    box, warnings = padded_box(fig,20)
    assert box == (486,230,931,427)
    assert box[3] < fig.caption_bbox_norm[1]
    assert box[3] >= fig.bbox_norm[3]
    assert not warnings


def test_padding_20_does_not_shrink_raw_box_when_caption_is_near():
    # Figure annotations and equations at the raw boundary must survive.
    fig = _figure(raw=(108,92,440,310),caption=(108,315,440,380),nearby=())
    box, _ = padded_box(fig,20)
    assert box == (88,72,460,310)
    assert box[3] >= fig.bbox_norm[3]


def test_normal_padding_when_caption_is_far_away():
    box, _ = padded_box(_figure(caption=(506,530,911,560),nearby=()), 20)
    assert box == (486,230,931,441)


def test_caption_clearance_is_only_used_for_actual_caption_box():
    fig = _figure(caption=None,nearby=((506,433,911,475),))
    box, _ = padded_box(fig,20)
    assert box[3] == 432


def test_caption_outside_horizontal_range_does_not_limit_bottom():
    box, _ = padded_box(_figure(caption=(0,433,100,475),nearby=()),20)
    assert box[3] == 441


def test_overlapping_caption_disables_padding_rather_than_clipping_original():
    fig = _figure(caption=(506,419,911,475), nearby=())
    box, warnings = padded_box(fig,20)
    assert box == fig.bbox_norm
    assert warnings and 'disabled' in warnings[0]


def test_padding_zero_preserves_exact_model_crop():
    fig=_figure()
    assert padded_box(fig,0) == (fig.bbox_norm, ())


def test_unchanged_image_crop_does_not_include_caption_color():
    """Pixel-level regression on a manufactured page at the saved coordinates."""
    img = Image.new('RGB',(1000,1000),'white')
    draw=ImageDraw.Draw(img)
    draw.rectangle((506,250,911,421), fill=(80,170,80))
    # Fake caption starts slightly ABOVE the model-predicted caption bbox
    # (precisely the kind of coordinate uncertainty seen in YOLOv3).
    draw.rectangle((506,430,911,447), fill=(250,0,0))
    source=BytesIO(); img.save(source,format='PNG'); img.close()
    full={
       'markdown':'## Results\n\n[[FIGURE:figure-1]]\n\nFigure 1. Performance graph\n\nFurther discussion.',
       'figures':[{'id':'figure-1','kind':'chart','caption':'Figure 1. Performance graph',
          'bbox_norm':[506,250,911,421], 'caption_bbox_norm':[506,433,911,475],
          'nearby_text_bboxes_norm':[[506,500,739,513]]}]
    }
    page, assets, warnings=assemble_page(json.dumps(full),source.getvalue(),1,Settings(_env_file=None,vision_figure_padding_norm=20))
    assert len(assets)==1
    assert page.markdown.count('![Figure 1. Performance graph](images/p0001-figure-1.png)')==1
    assert page.markdown.count('Figure 1. Performance graph') == 2  # alt + caption; only one caption block
    assert assets[0].adjusted_bbox_norm[3] == 427
    assert sha256(assets[0].data).hexdigest() == assets[0].sha256
    with Image.open(BytesIO(assets[0].data)) as cropped:
        assert cropped.getpixel((cropped.width//2,cropped.height-1)) != (250,0,0)
        assert cropped.getpixel((cropped.width//2,cropped.height-15)) == (80,170,80)


def test_changed_fourth_parser_policy_invalidates_only_its_cache(monkeypatch):
    settings=Settings(_env_file=None)
    digest='a'*64
    new=fingerprint(digest,ParserChoice.vision_figures,settings)
    old_other=fingerprint(digest,ParserChoice.gemini,settings)
    monkeypatch.setattr(vision_figures,'PROMPT_VERSION','vision-full-v3.1-1')
    assert new != fingerprint(digest,ParserChoice.vision_figures,settings)
    assert old_other == fingerprint(digest,ParserChoice.gemini,settings)
