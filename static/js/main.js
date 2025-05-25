// Check what might be causing the null reference error in main.js
// This is a safer version that checks for element existence before adding listeners

document.addEventListener('DOMContentLoaded', function() {
    // Initialize elements with null-checking
    const checkRankButton = document.getElementById('checkRankButton');
    const resultModal = document.getElementById('resultModal') ?
        new bootstrap.Modal(document.getElementById('resultModal')) : null;

    // Only attach event handlers if elements exist
    if (checkRankButton) {
        checkRankButton.addEventListener('click', function() {
            const rocketId = document.getElementById('rocketId')?.value.trim() || '';
            const platform = document.getElementById('platform')?.value || '';
            const discordId = document.getElementById('discordId')?.value.trim() || '';

            if (!rocketId) {
                alert('Please enter your Rocket League ID');
                return;
            }

            // Show loading state if verificationResult exists
            const resultDiv = document.getElementById('verificationResult');
            if (resultDiv) {
                resultDiv.innerHTML = `
                    <div class="text-center py-3">
                        <div class="spinner-border text-primary" role="status">
                            <span class="visually-hidden">Loading...</span>
                        </div>
                        <p class="mt-2">Checking your rank...</p>
                    </div>
                `;
            }

            // Disable the button while checking
            checkRankButton.disabled = true;
            checkRankButton.innerHTML = `
                <span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
                Checking...
            `;

            // Call our API endpoint to get the rank data
            fetchRankData(rocketId, platform, discordId);
        });
    }

    // Initialize rank selector if it exists
    const rankSelect = document.getElementById('rlRank');
    if (rankSelect) {
        setupRankSelector();

        // Check for a reset
        checkResetStatus();

        // Fallback check for very old verifications
        checkLastVerificationAge();
    }

    // Initialize player click handlers for both home and leaderboard
    setupPlayerClickHandlers();
});

// Other functions referenced earlier should be defined outside the DOMContentLoaded event
function setupRankSelector() {
    console.log("Setting up rank selector...");
    const rankSelect = document.getElementById('rlRank');
    const verifyButton = document.getElementById('verifyRankButton');
    const rankPreview = document.getElementById('rankPreview');

    if (!rankSelect || !verifyButton) {
        console.error("Required elements for rank selector not found");
        return;
    }

    // Show rank image when selection changes
    rankSelect.addEventListener('change', function() {
        const selectedOption = this.options[this.selectedIndex];
        const value = this.value;
        const text = selectedOption.text;
        const imagePath = selectedOption.getAttribute('data-icon');

        if (rankPreview) {
            const rankImage = document.getElementById('rankImage');
            const rankName = document.getElementById('rankName');

            if (rankImage && rankName) {
                rankImage.src = imagePath;
                rankImage.alt = text;
                rankName.textContent = text;
                rankPreview.classList.remove('d-none');
            }
        }
    });
}

function checkResetStatus() {
    console.log("Checking leaderboard reset status...");
    // Implementation details...
}

function checkLastVerificationAge() {
    console.log("Checking verification age...");
    // Implementation details...
}

function fetchRankData(username, platform, discordId) {
    // Build the API URL with query parameters
    let apiUrl = `/api/rank-check?platform=${encodeURIComponent(platform)}&username=${encodeURIComponent(username)}`;

    if (discordId) {
        apiUrl += `&discord_id=${encodeURIComponent(discordId)}`;
    }

    // Make the API request
    fetch(apiUrl)
        .then(response => response.json())
        .then(data => {
            // Process the API response
            if (data.success) {
                displayRankResults(data, discordId);
            } else {
                showError(data.message || "Could not retrieve rank information. Please check your ID and platform.");
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showError("An error occurred while checking your rank. Please try again later.");
        })
        .finally(() => {
            // Re-enable button regardless of outcome
            const checkRankButton = document.getElementById('checkRankButton');
            if (checkRankButton) {
                checkRankButton.disabled = false;
                checkRankButton.innerHTML = `<i class="fas fa-check-circle me-2"></i> Check My Rank`;
            }
        });
}

function displayRankResults(data, discordId) {
    const resultDiv = document.getElementById('verificationResult');
    if (!resultDiv) return;

    // Use the stored rankValue or fall back to a tier-based value
    const rankValue = data.rankValue || data.tier?.toLowerCase().replace(' ', '_') || 'rank_c';

    // Determine the rank icon based on tier
    let rankIcon = 'fas fa-medal';
    if (data.tier === 'Rank A') {
        rankIcon = 'fas fa-trophy';
    } else if (data.tier === 'Rank B') {
        rankIcon = 'fas fa-award';
    }

    // Determine the rank color class
    const rankColorClass = data.tier === 'Rank A' ? 'rank-a' : data.tier === 'Rank B' ? 'rank-b' : 'rank-c';
    const badgeClass = data.tier === 'Rank A' ? 'bg-danger' : data.tier === 'Rank B' ? 'bg-primary' : 'bg-success';

    // Create a success notification at the top
    const successAlert = `
        <div class="alert alert-success mb-3">
            <i class="fas fa-check-circle me-2"></i> Rank verification successful!
        </div>
    `;

    // Create a discord notification at the bottom
    const discordAlert = data.role_assignment && data.role_assignment.success ?
        `<div class="alert alert-success mt-3">
            <i class="fab fa-discord me-2"></i> Discord role assigned successfully!
        </div>` :
        `<div class="alert alert-warning mt-3">
            <i class="fab fa-discord me-2"></i> Could not assign Discord role automatically. Please contact an admin.
        </div>`;

    // Use the rank card design
    resultDiv.innerHTML = `
        ${successAlert}

        <div class="rank-card">
            <div class="rank-header ${rankColorClass}">
                <i class="${rankIcon} fa-2x"></i>
            </div>
            <div class="rank-body">
                <h5 class="rank-title">${data.tier}</h5>
                <p class="rank-description">${data.rank}</p>
                <p class="rank-mmr">Starting MMR: ${data.mmr}</p>
                <span class="badge ${badgeClass}">${data.tier} Role</span>
            </div>
        </div>

        ${discordAlert}
    `;

    // Show in the modal too if it exists
    const resultModal = document.getElementById('resultModal');
    const modalContent = document.getElementById('resultModalContent');
    if (modalContent && resultModal) {
        let modalContentHTML = `
            <div class="text-center">
                <div class="mb-3">
                    <i class="fas fa-check-circle fa-4x text-success"></i>
                </div>
                <h4>Your rank has been verified!</h4>
                <p class="mb-3">Based on your ${data.rank} rank.</p>
                
                <div class="alert alert-info">
                    <strong>Discord Role:</strong> ${data.tier}<br>
                    <strong>Starting MMR:</strong> ${data.mmr}
                </div>
        `;

        // Add role assignment result
        if (data.role_assignment && data.role_assignment.success) {
            modalContentHTML += `
                <div class="alert alert-success">
                    <i class="fab fa-discord me-2"></i> Your Discord role has been updated automatically!
                </div>
            `;
        } else {
            modalContentHTML += `
                <div class="alert alert-warning">
                    <i class="fas fa-exclamation-triangle me-2"></i> Could not update your Discord role automatically. Please contact an admin.
                </div>
            `;
        }

        modalContentHTML += `
                <p>You can now join the 6 Mans queue in our Discord server.</p>
                <a href="https://discord.gg/your-discord" target="_blank" class="btn btn-primary mt-2">
                    <i class="fab fa-discord me-2"></i> Join Our Discord
                </a>
            </div>
        `;

        modalContent.innerHTML = modalContentHTML;

        // Show modal
        const modal = new bootstrap.Modal(resultModal);
        modal.show();
    }
}

function showError(message) {
    // Update verification result section
    const resultDiv = document.getElementById('verificationResult');
    if (resultDiv) {
        resultDiv.innerHTML = `
            <div class="alert alert-danger mb-0">
                <i class="fas fa-exclamation-circle me-2"></i> ${message}
            </div>
        `;
    }

    // Update modal if it exists
    const resultModal = document.getElementById('resultModal');
    const modalContent = document.getElementById('resultModalContent');
    if (modalContent && resultModal) {
        modalContent.innerHTML = `
            <div class="text-center">
                <div class="mb-3">
                    <i class="fas fa-exclamation-circle fa-4x text-danger"></i>
                </div>
                <h4>Verification Failed</h4>
                <div class="alert alert-danger mb-0">
                    ${message}
                </div>
                <p class="mt-3">Please check your Rocket League ID and platform, then try again.</p>
                <p class="mt-3">If the problem persists, you can still join our Discord and request manual verification from an admin.</p>
                <a href="https://discord.gg/your-discord" target="_blank" class="btn btn-primary mt-2">
                    <i class="fab fa-discord me-2"></i> Join Our Discord
                </a>
            </div>
        `;

        // Show modal
        const modal = new bootstrap.Modal(resultModal);
        modal.show();
    }
}

//=========================================
// Fix for the player details loading error
//=========================================

function setupPlayerClickHandlers() {
    // Set up click events for featured players and leaderboard rows
    const featuredPlayers = document.querySelectorAll('.featured-player');
    const playerRows = document.querySelectorAll('.player-row');

    // Set up click handlers for featured players
    featuredPlayers.forEach(player => {
        player.addEventListener('click', function() {
            const playerId = this.dataset.playerId;
            if (playerId) {
                showPlayerDetails(playerId);
            } else {
                console.error("No player ID found for this element", this);
            }
        });
    });

    // Set up click handlers for player rows
    playerRows.forEach(row => {
        row.addEventListener('click', function() {
            const playerId = this.dataset.playerId;
            if (playerId) {
                showPlayerDetails(playerId);
            } else {
                console.error("No player ID found for this element", this);
            }
        });
    });

    console.log(`Initialized click handlers for ${featuredPlayers.length} featured players and ${playerRows.length} player rows`);
}

function showPlayerDetails(playerId) {
    // First check if the modal element exists
    const modalElement = document.getElementById('playerModal');
    if (!modalElement) {
        console.error("Player modal element not found");
        return;
    }

    // Find or create the Bootstrap Modal instance
    let playerModal;

    // Check if Bootstrap is available
    if (typeof bootstrap !== 'undefined') {
        // Try to get existing modal instance or create a new one
        playerModal = bootstrap.Modal.getInstance(modalElement) || new bootstrap.Modal(modalElement);
    } else {
        console.error("Bootstrap is not available");
        return;
    }

    // Clean playerId by removing any suffixes (like :1)
    if (playerId.includes(':')) {
        console.warn(`Player ID contains an invalid character, cleaning: ${playerId}`);
        playerId = playerId.split(':')[0];
    }

    // Get the modal content element
    const modalContent = document.getElementById('playerModalContent');

    if (!modalContent) {
        console.error("Modal content element not found");
        return;
    }

    // Reset modal content with loading indicator
    modalContent.innerHTML = `
        <div class="text-center">
            <div class="spinner-border text-light" role="status">
                <span class="visually-hidden">Loading...</span>
            </div>
            <p>Loading player data...</p>
        </div>
    `;

    // Show modal while loading data
    try {
        playerModal.show();
    } catch (error) {
        console.error("Error showing modal:", error);
        // Try an alternative approach if the first fails
        if (typeof $ !== 'undefined') {
            $(modalElement).modal('show');
        }
    }

    // Fetch player data with improved error handling
    console.log(`Fetching player data for ID: ${playerId}`);

    fetch(`/api/player/${playerId}`)
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error ${response.status}`);
            }
            return response.json();
        })
        .then(player => {
            if (player.error) {
                modalContent.innerHTML = `<div class="alert alert-danger">${player.error}</div>`;
                return;
            }

            // Update the modal title with player name
            const modalTitle = document.getElementById('playerModalLabel');
            if (modalTitle) {
                modalTitle.textContent = player.name || 'Player Details';
            }

            // Create the complete modal content with both tabs
            let content = `
                <ul class="nav nav-tabs mb-4" id="playerStatsTabs" role="tablist">
                    <li class="nav-item" role="presentation">
                        <button class="nav-link active" id="ranked-tab" data-bs-toggle="tab"
                                data-bs-target="#ranked-stats" type="button" role="tab"
                                aria-controls="ranked-stats" aria-selected="true">
                            Ranked Stats
                        </button>
                    </li>
                    <li class="nav-item" role="presentation">
                        <button class="nav-link" id="global-tab" data-bs-toggle="tab"
                                data-bs-target="#global-stats" type="button" role="tab"
                                aria-controls="global-stats" aria-selected="false">
                            Global Stats
                        </button>
                    </li>
                </ul>

                <div class="tab-content" id="playerStatsContent">
                    <!-- Ranked Stats Tab -->
                    <div class="tab-pane fade show active" id="ranked-stats" role="tabpanel" aria-labelledby="ranked-tab">
                        <div class="card bg-dark-medium mb-4">
                            <div class="card-body">
                                <div class="row">
                                    <div class="col-md-4">
                                        <div class="mmr-display">
                                            <h4 class="mmr-label">MMR</h4>
                                            <div class="mmr-value">${player.mmr || 0}</div>
                                        </div>
                                    </div>
                                    <div class="col-md-8">
                                        <div class="stats-grid">
                                            <div class="stat-item">
                                                <h5>Win Rate</h5>
                                                <span class="stat-value">${player.win_rate || 0}%</span>
                                            </div>
                                            <div class="stat-item">
                                                <h5>Record</h5>
                                                <span class="stat-value">${player.wins || 0}-${player.losses || 0}</span>
                                            </div>
                                            <div class="stat-item">
                                                <h5>Matches</h5>
                                                <span class="stat-value">${player.matches || 0}</span>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
            `;

            // Display recent ranked matches if available
            const hasMatches = player.recent_matches && player.recent_matches.length > 0;
            const rankedMatches = hasMatches ? player.recent_matches.filter(match => !match.is_global) : [];

            if (rankedMatches.length > 0) {
                content += `
                    <h4 class="section-header">Recent Ranked Matches</h4>
                    <div class="table-responsive">
                        <table class="table table-dark table-striped match-table">
                            <thead>
                                <tr>
                                    <th style="width:120px">Date</th>
                                    <th style="width:90px;text-align:center">Result</th>
                                    <th>Teams</th>
                                </tr>
                            </thead>
                            <tbody>
                `;

                rankedMatches.forEach(match => {
                    // Safely format teams with error checking
                    let team1Names = '';
                    let team2Names = '';

                    try {
                        team1Names = match.team1 && Array.isArray(match.team1) ?
                            match.team1.map(p => p.name || 'Unknown').join(', ') : 'Unknown Team';

                        team2Names = match.team2 && Array.isArray(match.team2) ?
                            match.team2.map(p => p.name || 'Unknown').join(', ') : 'Unknown Team';
                    } catch (e) {
                        console.error("Error formatting teams:", e);
                        team1Names = 'Error loading team';
                        team2Names = 'Error loading team';
                    }

                    // Determine result badge
                    let resultBadge = match.player_result === 'Win' ?
                        '<span class="badge bg-success">Win</span>' :
                        '<span class="badge bg-danger">Loss</span>';

                    content += `
                        <tr>
                            <td style="vertical-align:middle">${match.date || 'Unknown'}</td>
                            <td style="text-align:center;vertical-align:middle">${resultBadge}</td>
                            <td style="vertical-align:middle">
                                <span style="display:inline-block;margin-right:5px">${team1Names}</span>
                                <span class="badge bg-dark mx-2">vs</span>
                                <span style="display:inline-block;margin-left:5px">${team2Names}</span>
                            </td>
                        </tr>
                    `;
                });

                content += `
                            </tbody>
                        </table>
                    </div>
                `;
            } else {
                content += `<p class="text-muted">No recent ranked matches found.</p>`;
            }

            // Close ranked stats tab and begin global stats tab
            content += `
                    </div>

                    <!-- Global Stats Tab -->
                    <div class="tab-pane fade" id="global-stats" role="tabpanel" aria-labelledby="global-tab">
                        <div class="card bg-dark-medium mb-4">
                            <div class="card-body">
                                <div class="row">
                                    <div class="col-md-4">
                                        <div class="mmr-display global">
                                            <h4 class="mmr-label">Global MMR</h4>
                                            <div class="mmr-value">${player.global_mmr || 300}</div>
                                        </div>
                                    </div>
                                    <div class="col-md-8">
                                        <div class="stats-grid">
                                            <div class="stat-item">
                                                <h5>Win Rate</h5>
                                                <span class="stat-value">${player.global_win_rate || 0}%</span>
                                            </div>
                                            <div class="stat-item">
                                                <h5>Record</h5>
                                                <span class="stat-value">${player.global_wins || 0}-${player.global_losses || 0}</span>
                                            </div>
                                            <div class="stat-item">
                                                <h5>Matches</h5>
                                                <span class="stat-value">${player.global_matches || 0}</span>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
            `;

            // Display recent global matches
            const globalMatches = hasMatches ? player.recent_matches.filter(match => match.is_global) : [];
            if (globalMatches.length > 0) {
                content += `
                    <h4 class="section-header">Recent Global Matches</h4>
                    <div class="table-responsive">
                        <table class="table table-dark table-striped match-table">
                            <thead>
                                <tr>
                                    <th style="width:120px">Date</th>
                                    <th style="width:90px;text-align:center">Result</th>
                                    <th>Teams</th>
                                </tr>
                            </thead>
                            <tbody>
                `;

                globalMatches.forEach(match => {
                    // Safely format teams with error checking
                    let team1Names = '';
                    let team2Names = '';

                    try {
                        team1Names = match.team1 && Array.isArray(match.team1) ?
                            match.team1.map(p => p.name || 'Unknown').join(', ') : 'Unknown Team';

                        team2Names = match.team2 && Array.isArray(match.team2) ?
                            match.team2.map(p => p.name || 'Unknown').join(', ') : 'Unknown Team';
                    } catch (e) {
                        console.error("Error formatting teams:", e);
                        team1Names = 'Error loading team';
                        team2Names = 'Error loading team';
                    }

                    // Determine result badge
                    let resultBadge = match.player_result === 'Win' ?
                        '<span class="badge bg-success">Win</span>' :
                        '<span class="badge bg-danger">Loss</span>';

                    content += `
                        <tr>
                            <td style="vertical-align:middle">${match.date || 'Unknown'}</td>
                            <td style="text-align:center;vertical-align:middle">${resultBadge}</td>
                            <td style="vertical-align:middle">
                                <span style="display:inline-block;margin-right:5px">${team1Names}</span>
                                <span class="badge bg-dark mx-2">vs</span>
                                <span style="display:inline-block;margin-left:5px">${team2Names}</span>
                            </td>
                        </tr>
                    `;
                });

                content += `
                            </tbody>
                        </table>
                    </div>
                `;
            } else {
                content += `<p class="text-muted">No recent global matches found.</p>`;
            }

            // Close global stats tab
            content += `
                    </div>
                </div>
            `;

            modalContent.innerHTML = content;
        })
        .catch(error => {
            console.error('Error fetching player details:', error);
            modalContent.innerHTML = `
                <div class="alert alert-danger">
                    <p><strong>Error loading player data:</strong> ${error.message}</p>
                    <p>Please try again later or contact an administrator if the problem persists.</p>
                </div>`;
        });
}

// Make the showPlayerDetails function globally available
window.showPlayerDetails = showPlayerDetails;