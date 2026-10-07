import AppKit
import LatentCore
import SwiftUI

extension LibraryModel {
    var importActive: Bool { photoImports.contains(where: \.active) }
    var embeddingActive: Bool { embeddingRuns.contains { $0.status == "running" } }
    var workflowWritable: Bool { workspaceWritable && workflowDataID == service.identity?.dataId && !workflowBusy }
    var currentImport: PhotoImport? {
        guard case .importBatch(let id, _) = source else { return nil }
        return photoImports.first { $0.id == id }
    }

    func openImports(tab: String = "imports") { importTab = tab; showingImports = true }

    func monitorImports() async {
        while !Task.isCancelled {
            await refreshImports()
            do { try await Task.sleep(for: .seconds(importActive || embeddingActive || validationRunning || showingImports ? 2 : 8)) }
            catch { return }
        }
    }

    func refreshImports() async {
        guard service.isConnected, !workflowBusy, let dataID = service.identity?.dataId else { return }
        workflowGeneration += 1
        let generation = workflowGeneration
        do {
            async let imports = client.imports()
            async let embeddings = client.embeddingRuns()
            let (batches, runs) = try await (imports, embeddings)
            guard service.isConnected, service.identity?.dataId == dataID,
                  batches.dataId == dataID, runs.dataId == dataID,
                  workflowGeneration == generation, !workflowBusy else { return }
            let changed = workflowDataID == dataID && (
                batches.batches.reduce(0) { $0 + $1.previewReady } != photoImports.reduce(0) { $0 + $1.previewReady }
                || runs.runs.reduce(0) { $0 + $1.succeeded } != embeddingRuns.reduce(0) { $0 + $1.succeeded })
            if workflowDataID != dataID { embeddingReview = nil; workflowError = nil }
            photoImports = batches.batches
            embeddingRuns = runs.runs
            workflowDataID = dataID
            if changed { await refreshCatalogSummary() }
            await refreshSearchEngines(dataID: dataID, generation: generation)
        } catch {
            if generation == workflowGeneration, service.identity?.dataId == dataID {
                workflowError = error.localizedDescription
            }
        }
    }

    /// Older services lack engine choices; imports and AI Search keep working without them.
    private func refreshSearchEngines(dataID: String, generation: Int) async {
        let engines = try? await client.searchEngines()
        guard service.identity?.dataId == dataID, workflowGeneration == generation, !workflowBusy else { return }
        searchEngines = engines?.dataId == dataID ? engines : nil
    }

    var validationRunning: Bool { searchEngines?.backends.contains { $0.validation?.status == "running" } == true }

    func validateEngine(_ engine: SearchEngine) {
        guard workflowWritable, let dataID = workflowDataID else { return }
        performWorkflow(dataID: dataID) {
            try await self.client.validateSearchEngine(engine.key, dataID: dataID)
            let engines = try await self.client.searchEngines()
            if engines.dataId == dataID { self.searchEngines = engines }
        }
    }

    func activateEngine(_ engine: SearchEngine) {
        guard workflowWritable, let dataID = workflowDataID else { return }
        performWorkflow(dataID: dataID) {
            let engines = try await self.client.activateSearchEngine(engine, dataID: dataID)
            guard engines.dataId == dataID, self.service.identity?.dataId == dataID else { return }
            self.searchEngines = engines
            await self.searchEngineChanged()
        }
    }

    func saveGeminiKey(_ key: String) async throws {
        guard workflowWritable, let dataID = workflowDataID else {
            throw ServiceConnectionError("Reconnect to Latent before saving the key.")
        }
        let engines = try await client.saveGeminiKey(key, dataID: dataID)
        guard engines.dataId == dataID, service.identity?.dataId == dataID else { return }
        searchEngines = engines
        await refreshCatalogSummary()
    }

    func removeGeminiKey() {
        guard workflowWritable, let dataID = workflowDataID else { return }
        performWorkflow(dataID: dataID) {
            let engines = try await self.client.removeGeminiKey(dataID: dataID)
            guard engines.dataId == dataID, self.service.identity?.dataId == dataID else { return }
            self.searchEngines = engines
        }
    }

    func chooseImportFolders() {
        guard workflowWritable, !importActive, let dataID = workflowDataID else { return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        panel.prompt = "Review Import"
        panel.message = "Choose one or more camera folders. Originals stay in place."
        panel.begin { response in
            guard response == .OK, !panel.urls.isEmpty, self.service.identity?.dataId == dataID else { return }
            let paths = panel.urls.map(\.path)
            self.performWorkflow(dataID: dataID) {
                let response = try await self.client.prepareImport(id: UUID().uuidString.lowercased(), paths: paths, dataID: dataID)
                guard response.dataId == dataID, self.service.identity?.dataId == dataID else { return }
                self.photoImports.removeAll { $0.id == response.batch.id }
                self.photoImports.insert(response.batch, at: 0)
            }
        }
    }

    func chooseArchiveDestination(_ batch: PhotoImport) {
        guard workflowWritable, let dataID = workflowDataID else { return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.prompt = "Choose Archive"
        panel.message = "Choose the archive folder on a disk or mounted CloudDrive. Copying starts only after your confirmation."
        panel.begin { response in
            guard response == .OK, let url = panel.url, self.service.identity?.dataId == dataID else { return }
            self.importAction(batch, action: "destination", path: url.path)
        }
    }

    func importAction(_ batch: PhotoImport, action: String, path: String? = nil) {
        guard workflowWritable, let dataID = workflowDataID else { return }
        performWorkflow(dataID: dataID) {
            let response = try await self.client.importAction(batch, action: action, dataID: dataID, path: path)
            guard response.dataId == dataID, self.service.identity?.dataId == dataID else { return }
            if let index = self.photoImports.firstIndex(where: { $0.id == batch.id }) { self.photoImports[index] = response.batch }
        }
    }

    func reviewEmbeddings(scope: String, importID: String? = nil, backend: String? = nil) {
        guard workflowWritable, let dataID = workflowDataID else { return }
        let ids = scope == "selected" ? selectedPhotos.map(\.id) : nil
        performWorkflow(dataID: dataID) {
            let response = try await self.client.prepareEmbeddings(id: UUID().uuidString.lowercased(), scope: scope,
                ids: ids, importID: importID, backend: backend, dataID: dataID)
            guard response.dataId == dataID, self.service.identity?.dataId == dataID else { return }
            self.embeddingRuns.insert(response.run, at: 0)
            self.embeddingReview = response.run
        }
    }

    func embeddingAction(_ run: PhotoEmbeddingRun, action: String) {
        guard workflowWritable, let dataID = workflowDataID else { return }
        performWorkflow(dataID: dataID) {
            let response = try await self.client.embeddingAction(run, action: action, dataID: dataID)
            guard response.dataId == dataID, self.service.identity?.dataId == dataID else { return }
            if let index = self.embeddingRuns.firstIndex(where: { $0.id == run.id }) { self.embeddingRuns[index] = response.run }
            if action != "pause" { self.embeddingReview = nil }
        }
    }

    private func performWorkflow(dataID: String, operation: @escaping @MainActor () async throws -> Void) {
        guard !workflowBusy, service.identity?.dataId == dataID else { return }
        workflowBusy = true
        workflowGeneration += 1
        workflowError = nil
        Task {
            defer { workflowBusy = false }
            do { try await operation() }
            catch { if service.identity?.dataId == dataID { workflowError = error.localizedDescription } }
        }
    }
}

struct ImportsView: View {
    @Bindable var model: LibraryModel
    @Environment(\.dismiss) private var dismiss
    @State private var archiveReview: PhotoImport?
    @State private var embeddingScope = "incremental"
    @State private var chosenEngine: String?
    @State private var editingGeminiKey = false
    @State private var confirmingKeyRemoval = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text(model.importTab == "imports" ? "Imports" : "AI Search").font(.title2.bold())
                    Text(model.importTab == "imports"
                         ? "Build local previews and archive your originals."
                         : "Choose a search engine, then review before generating photo embeddings.")
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            SheetConnectionBanner(model: model)
            Picker("Workflow", selection: $model.importTab) {
                Text("Imports").tag("imports")
                Text("AI Search").tag("embeddings")
            }.pickerStyle(.segmented)
            if let error = model.workflowError {
                Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled)
            }
            if model.importTab == "imports" { imports } else { embeddings }
        }
        .padding(26).frame(width: 840, height: 730)
        .preferredColorScheme(.dark)
        .task { await model.refreshImports() }
        .sheet(item: $archiveReview) { batch in archiveConfirmation(batch) }
        .sheet(item: $model.embeddingReview) { run in EmbeddingConfirmation(model: model, run: run) }
        .sheet(isPresented: $editingGeminiKey) { GeminiKeySheet(model: model) }
        .confirmationDialog("Remove the Gemini API key from your login Keychain?", isPresented: $confirmingKeyRemoval) {
            Button("Remove Key", role: .destructive) { model.removeGeminiKey() }
        } message: {
            Text("Gemini search and indexing stop working until you add a key again.")
        }
    }

    private var imports: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("New photos").font(.headline)
                    Text("Add camera cards or folders. Local previews use no AI.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Add folders…") { model.chooseImportFolders() }
                    .buttonStyle(.glassProminent).disabled(!model.workflowWritable || model.importActive)
            }
            if model.photoImports.isEmpty {
                ContentUnavailableView("Ready for your next shoot", systemImage: "camera.on.rectangle",
                    description: Text("Select both camera folders in one import. RAW, JPEG, HEIF and sidecars stay together."))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        ForEach(model.photoImports) { batch in importCard(batch) }
                    }.padding(.trailing, 4)
                }
            }
            Text("Existing previews are reused. Originals stay in place. AI Search runs only after you review and confirm it.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func importCard(_ batch: PhotoImport) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(batch.title).font(.headline)
                    Text("\(batch.photoCount.formatted()) photos · \(batch.sidecarCount.formatted()) \(batch.sidecarCount == 1 ? "sidecar" : "sidecars") · \(bytes(batch.bytesTotal))")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Text(batch.label).font(.caption).foregroundStyle(batch.status == "archived" ? .green : .secondary)
            }
            ForEach(batch.sources, id: \.id) { source in
                Text(source.folder.path).font(.caption.monospaced()).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle).textSelection(.enabled)
            }
            if batch.otherCount > 0 || batch.ignored > 0 {
                Text("\(batch.otherCount.formatted()) other files included · \(batch.ignored.formatted()) hidden or excluded files skipped")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if batch.active {
                ProgressView(value: Double(batch.phase == "preview" ? batch.previewReady + batch.previewFailed : batch.archived + batch.skipped),
                             total: Double(max(1, batch.phase == "preview" ? batch.photoCount : batch.fileCount)))
            }
            HStack {
                Label("\(batch.previewReady.formatted()) / \(batch.photoCount.formatted()) previews", systemImage: "photo")
                Spacer()
                if batch.archived > 0 || batch.phase == "archive" {
                    Label("\(batch.archived.formatted()) / \(batch.fileCount.formatted()) verified", systemImage: "checkmark.shield")
                }
            }.font(.caption).foregroundStyle(.secondary)
            if !batch.currentPath.isEmpty {
                Text(batch.currentPath).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            if let error = batch.error { Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled) }
            if batch.previewFailed > 0 {
                DisclosureGroup("\(batch.previewFailed) previews need attention") {
                    ForEach(Array(batch.failures.enumerated()), id: \.offset) { _, failure in
                        Text("\(failure.name): \(failure.message)").font(.caption).textSelection(.enabled)
                    }
                }.font(.caption)
            }
            HStack {
                if batch.active {
                    Button("Pause") { model.importAction(batch, action: "pause") }
                } else if batch.status == "prepared" {
                    Button("Build previews") { model.importAction(batch, action: "start") }.buttonStyle(.glassProminent)
                        .disabled(model.importActive)
                } else if ["paused", "interrupted", "waiting_for_cloud"].contains(batch.status)
                    || (batch.status == "needs_attention" && batch.phase == "archive") {
                    Button("Resume") { model.importAction(batch, action: "resume") }
                        .disabled(model.importActive)
                }
                if batch.previewFailed > 0 && !batch.active {
                    Button("Retry previews") { model.importAction(batch, action: "retry_previews") }.disabled(model.importActive)
                }
                Button("View photos") {
                    model.navigate(to: .importBatch(id: batch.id, name: batch.title)); dismiss()
                }.disabled(batch.previewReady == 0)
                Spacer()
                Button("Review AI search…") { model.reviewEmbeddings(scope: "import", importID: batch.id) }
                    .disabled(batch.previewReady == 0 || model.embeddingActive)
            }.buttonStyle(.glass).disabled(!model.workflowWritable)
            Divider()
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Archive originals").font(.subheadline.bold())
                    Text(batch.destination?.path ?? "Choose an archive folder when you are ready.")
                        .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    if batch.destination != nil {
                        Text(batch.destination?.cloud == nil ? "Disk copy, verified by checksum." : "CloudDrive copy, confirmed against the cloud file hash.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if batch.archived == 0 && batch.copied == 0 && !batch.active {
                    Button("Choose…") { model.chooseArchiveDestination(batch) }.disabled(model.importActive)
                }
                if batch.status != "archived", batch.destination != nil, batch.phase == "preview", batch.previewsFinished {
                    Button("Review archive…") { archiveReview = batch }.disabled(model.importActive)
                }
            }.buttonStyle(.glass).disabled(!model.workflowWritable)
            if batch.status == "archived" {
                Text("Verified copies: \(bytes(batch.archivedBytes)). Source files remain in their original folders.")
                    .font(.caption).foregroundStyle(.green)
            }
        }
        .padding(18).frame(maxWidth: .infinity, alignment: .leading)
        .background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(.white.opacity(0.08)))
    }

    private func archiveConfirmation(_ batch: PhotoImport) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Archive \(batch.title)?").font(.title2.bold())
            Text("Copy \(batch.fileCount.formatted()) files (\(bytes(batch.bytesTotal))) to:")
            Text(batch.destination?.path ?? "").font(.callout.monospaced()).textSelection(.enabled)
            Text("Photos are organized by capture date, then camera folder. Sidecars follow their originals. Existing files are preserved and different versions receive a new name.")
                .foregroundStyle(.secondary)
            if batch.undated > 0 {
                Text("\(batch.undated) photos have no capture date and will go into Undated.").foregroundStyle(.orange)
            }
            Text(batch.destination?.cloud == nil
                 ? "Each disk copy is verified by checksum. Keep the source connected until the batch finishes."
                 : "Each cloud copy must be confirmed before the next file. Keep CloudDrive and the source connected; pending uploads can be resumed.")
                .foregroundStyle(.secondary)
            Text("Originals remain in place. AI search is a separate step.").font(.callout)
            HStack {
                Button("Cancel") { archiveReview = nil }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Copy & verify") { model.importAction(batch, action: "archive"); archiveReview = nil }
                    .buttonStyle(.glassProminent).disabled(!model.workflowWritable || model.importActive)
            }
        }.padding(28).frame(width: 580)
    }

    private var reviewEngine: SearchEngine? {
        let engines = model.searchEngines
        return engines?.backends.first { $0.key == (chosenEngine ?? engines?.active) }
    }

    private var engines: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Search engine").font(.headline)
            if let list = model.searchEngines {
                ForEach(list.backends) { engine in
                    engineRow(engine, switchable: list.switchable)
                    if engine.id != list.backends.last?.id { Divider() }
                }
            } else {
                Text("This Latent service does not offer a choice of search engine. Quit and reopen Latent to update it.")
                    .font(.callout).foregroundStyle(.secondary)
            }
        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 14))
    }

    private func engineRow(_ engine: SearchEngine, switchable: Bool) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: engine.local ? "laptopcomputer" : "cloud").foregroundStyle(.secondary)
                    .frame(width: 24, alignment: .leading)
                Text(engine.displayName).font(.callout.weight(.semibold))
                if engine.active {
                    Text("In use").font(.caption2.weight(.semibold))
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(.green.opacity(0.2), in: Capsule())
                }
                Spacer()
                Text(engine.coverage).font(.caption).foregroundStyle(.secondary)
            }
            Text(engine.local
                 ? "Encodes on this Mac; nothing is uploaded. Image search can't be refined with words yet."
                 : "Sends small previews and search text to Google. Charged per photo.")
                .font(.caption).foregroundStyle(.secondary)
            if let problem = engine.availability {
                Text(problem).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
            }
            if engine.key == "gemini" {
                geminiKeyControls(engine)
            } else if engine.local, engine.availability == nil {
                if let runtime = engine.runtime {
                    Text("Model: \(runtime.label)").font(.caption).foregroundStyle(.secondary)
                    if let error = runtime.error {
                        Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                    }
                }
            }
            if engine.requiresValidation, engine.index.indexedAssets == 0 {
                Text("Generate its index below, then validate it before switching.")
                    .font(.caption).foregroundStyle(.secondary)
            } else if let validation = engine.validation {
                HStack(spacing: 6) {
                    if validation.status == "running" { ProgressView().controlSize(.mini) }
                    Text(validation.label).font(.caption)
                        .foregroundStyle(validation.status == "passed" ? Color.secondary : Color.orange)
                }
                ForEach(validation.failedChecks) { check in
                    Text(check.detail).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                }
                if let error = validation.error {
                    Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                }
            }
            if engine.requiresValidation || (switchable && !engine.active) {
                HStack {
                    if engine.requiresValidation {
                        Button(engine.validation?.status == "passed" ? "Validate again" : "Validate index") {
                            model.validateEngine(engine)
                        }
                        .help("Checks stored vectors and re-encodes sample photos before this engine can be used")
                        .disabled(!model.workflowWritable || !engine.canValidate || model.embeddingActive)
                    }
                    if switchable && !engine.active {
                        Button("Use for search") { model.activateEngine(engine) }
                            .buttonStyle(.glassProminent)
                            .help(engine.coverage)
                            .disabled(!model.workflowWritable || !engine.canActivate)
                        if engine.canActivate, engine.index.indexedAssets < engine.index.totalAssets {
                            Text("Only indexed photos will appear in search.").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }.controlSize(.small).padding(.top, 2)
            }
        }.padding(.vertical, 4)
    }

    private func geminiKeyControls(_ engine: SearchEngine) -> some View {
        HStack(spacing: 10) {
            switch engine.credentialSource {
            case "environment":
                Text("API key from the service environment").font(.caption).foregroundStyle(.secondary)
            case "keychain":
                Text("API key saved in your login Keychain").font(.caption).foregroundStyle(.secondary)
                Button("Change Key…") { editingGeminiKey = true }
                Button("Remove Key") { confirmingKeyRemoval = true }
            default:
                Button("Add API Key…") { editingGeminiKey = true }
            }
        }
        .controlSize(.small)
        .disabled(!model.workflowWritable)
    }

    private var embeddings: some View {
        VStack(alignment: .leading, spacing: 16) {
            engines
            VStack(alignment: .leading, spacing: 10) {
                Text("Generate photo embeddings").font(.headline)
                Text(reviewEngine?.local == true
                     ? "Build this engine's index on this Mac from small previews. Review the number of photos and estimated time before starting."
                     : "Enable text and image search using small previews sent to Gemini. Review the number of photos, upload size and estimated API cost before starting.")
                    .font(.callout).foregroundStyle(.secondary)
                HStack {
                    if let list = model.searchEngines {
                        Picker("Engine", selection: Binding(get: { chosenEngine ?? list.active }, set: { chosenEngine = $0 })) {
                            ForEach(list.backends) { Text($0.displayName).tag($0.key) }
                        }.fixedSize()
                    }
                    Picker("Scope", selection: $embeddingScope) {
                        Text("Incremental: missing embeddings").tag("incremental")
                        Text("All photos").tag("all")
                        Text("Selected photos (\(model.selectedPhotos.count))").tag("selected")
                    }
                    Button("Review…") { model.reviewEmbeddings(scope: embeddingScope, backend: reviewEngine?.key) }
                        .buttonStyle(.glassProminent)
                        .disabled(!model.workflowWritable || model.embeddingActive || (embeddingScope == "selected" && model.selectedPhotos.isEmpty))
                }
                Text("Existing embeddings are reused. Imports never start this step automatically.")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
            .background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 14))
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(model.embeddingRuns) { run in
                        VStack(alignment: .leading, spacing: 10) {
                            HStack {
                                Text(run.title).font(.headline)
                                Text(run.engineName).font(.caption).foregroundStyle(.secondary)
                                Spacer()
                                Text(run.label).font(.caption).foregroundStyle(.secondary)
                            }
                            Text("\(run.succeeded.formatted()) / \(run.toGenerate.formatted()) generated · \(run.reused.formatted()) already indexed")
                                .font(.callout).foregroundStyle(.secondary)
                            if run.status == "running" {
                                ProgressView(value: Double(run.succeeded), total: Double(max(1, run.toGenerate)))
                            }
                            if let error = run.error { Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled) }
                            if run.status == "running" {
                                Button(run.isLocal ? "Pause after current photo" : "Pause after current request") {
                                    model.embeddingAction(run, action: "pause")
                                }
                                    .disabled(!model.workflowWritable)
                            } else if run.status != "complete" {
                                Button(run.status == "prepared" ? "Review & confirm…" : "Review & resume…") {
                                    model.workflowError = nil; model.embeddingReview = run
                                }.disabled(!model.workflowWritable || model.embeddingActive)
                            }
                        }.padding(18).frame(maxWidth: .infinity, alignment: .leading)
                            .background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 14))
                    }
                }
            }
        }
    }

    private func bytes(_ value: Int64) -> String { ByteCountFormatter.string(fromByteCount: value, countStyle: .file) }
}

struct GeminiKeySheet: View {
    @Bindable var model: LibraryModel
    @Environment(\.dismiss) private var dismiss
    @State private var key = ""
    @State private var saving = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Gemini API Key").font(.title2.bold())
            Text("Latent saves the key in your login Keychain. It is sent only to the Gemini API, with indexing and search requests.")
                .font(.callout).foregroundStyle(.secondary)
            SecureField("Paste your API key", text: $key)
                .textFieldStyle(.roundedBorder)
                .onSubmit(save)
            Link("Get a key in Google AI Studio", destination: URL(string: "https://aistudio.google.com/apikey")!)
                .font(.callout)
            if let error { Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled) }
            HStack {
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button("Save") { save() }
                    .buttonStyle(.glassProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || saving)
            }
        }.padding(28).frame(width: 480)
    }

    private func save() {
        guard !saving, !key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        saving = true
        error = nil
        let value = key
        Task {
            defer { saving = false }
            do {
                try await model.saveGeminiKey(value)
                key = ""
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

private struct EmbeddingConfirmation: View {
    @Bindable var model: LibraryModel
    let run: PhotoEmbeddingRun
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text(run.remaining == 0 ? "These photos are ready for AI search" : "Generate embeddings?").font(.title2.bold())
            Grid(alignment: .leading, horizontalSpacing: 28, verticalSpacing: 10) {
                GridRow { Text("Scope").foregroundStyle(.secondary); Text(run.title) }
                GridRow { Text("Reviewed photos").foregroundStyle(.secondary); Text(run.selected.formatted()) }
                GridRow { Text("Already indexed").foregroundStyle(.secondary); Text((run.reused + run.succeeded).formatted()) }
                GridRow { Text("To generate").foregroundStyle(.secondary); Text(run.remaining.formatted()).bold() }
                GridRow { Text("Engine").foregroundStyle(.secondary); Text(run.engineName) }
                if run.isLocal {
                    GridRow { Text("Upload").foregroundStyle(.secondary); Text("None") }
                    if let seconds = run.remainingSeconds {
                        GridRow {
                            Text("Estimated time").foregroundStyle(.secondary)
                            Text(Duration.seconds(seconds), format: .units(allowed: [.hours, .minutes], width: .wide, maximumUnitCount: 2))
                        }
                    }
                } else {
                    GridRow { Text("Planned preview upload").foregroundStyle(.secondary); Text(ByteCountFormatter.string(fromByteCount: run.uploadBytes, countStyle: .file)) }
                    GridRow { Text("Estimated remaining cost").foregroundStyle(.secondary); Text(run.remainingCost, format: .currency(code: "USD").precision(.fractionLength(4))) }
                }
            }
            Text(run.isLocal
                 ? "Only the photos in this reviewed selection are included. Small previews are encoded on this Mac and nothing is uploaded. The model uses about 1.5 GB of memory while it is loaded; the time estimate is from an M2 Pro."
                 : "Only the photos in this reviewed selection are included. Small previews are sent to Gemini (\(run.model)); original files stay where they are. Actual API charges may vary, including retries.")
                .font(.callout).foregroundStyle(.secondary)
            if let error = model.workflowError { Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled) }
            HStack {
                Button("Not now") { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                if model.workflowBusy { ProgressView().controlSize(.small) }
                Button(run.remaining == 0 ? "Finish" : "Generate \(run.remaining.formatted()) \(run.remaining == 1 ? "photo" : "photos")") {
                    model.embeddingAction(run, action: run.status == "prepared" ? "start" : "resume")
                }.buttonStyle(.glassProminent).disabled(!model.workflowWritable || model.embeddingActive)
            }
        }.padding(28).frame(width: 580)
    }
}
