/* Helpers shared by the delivery library and the human-review dialog. */
function revisionForFile(track,file){
  if(!file?.revision)return null;
  return file.revision===track.revision?.revision?track.revision:
    (track.revision_history||[]).find(r=>r.revision===file.revision)||null;
}
