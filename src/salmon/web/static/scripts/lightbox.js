function zero_pad (nr) {
  return String(nr).length === 1 ? `0${nr}` : nr;
}

function clear_lightbox() {
  var lightbox = document.getElementById('lightbox');
  while (lightbox.firstChild) {
    lightbox.removeChild(lightbox.firstChild);
  }
  lightbox.style.display = 'none';
}
  

function show_lightbox(spec_id) {
  clear_lightbox();
  var lightbox = document.getElementById('lightbox');

  var static_dir = document.getElementById('static_dir').innerHTML;
  var url = `${static_dir}specs/${zero_pad(spec_id)} Zoom.png`;
  var img = document.createElement('img');
  img.setAttribute('src', url);
  img.setAttribute('id', 'lightbox_img');

  lightbox.appendChild(img);
  lightbox.style.display = 'block';
  lightbox.onclick = clear_lightbox;
}
