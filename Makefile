PREFIX ?= /usr/local
BINDIR = $(PREFIX)/bin
DATADIR = $(PREFIX)/share
APPID = io.github.byanurag.shuttermark

.PHONY: run check validate install uninstall

run:
	python3 shuttermark.py

check:
	python3 -m py_compile shuttermark.py
	python3 - <<'PY'
	import gi
	gi.require_version("Gtk", "4.0")
	gi.require_version("Gdk", "4.0")
	gi.require_version("GdkPixbuf", "2.0")
	import shuttermark as sm
	m = sm.Mark("Rectangle", [(10, 10), (50, 40)])
	assert sm.mark_bbox(m) is not None
	assert sm.mark_hit_test(m, 30, 25)
	assert not sm.mark_hit_test(m, 200, 200)
	print("check OK")
	PY

validate:
	desktop-file-validate $(APPID).desktop
	appstreamcli validate $(APPID).metainfo.xml || true

install:
	install -Dm755 shuttermark.py $(DESTDIR)$(BINDIR)/shuttermark
	install -Dm644 $(APPID).desktop $(DESTDIR)$(DATADIR)/applications/$(APPID).desktop
	install -Dm644 $(APPID).metainfo.xml $(DESTDIR)$(DATADIR)/metainfo/$(APPID).metainfo.xml

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/shuttermark
	rm -f $(DESTDIR)$(DATADIR)/applications/$(APPID).desktop
	rm -f $(DESTDIR)$(DATADIR)/metainfo/$(APPID).metainfo.xml
